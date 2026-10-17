"""策略工坊接口（PRD 3.8）。"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id, require_admin
from app.auth import core as auth_core
from app.db.session import get_db
from app.models import LLMProviderRow
from app.models.quant import (
    QuantPortfolioRow,
    QuantStrategyRow,
    QuantVersionRow,
    RealAccountRow,
    RealFillRow,
    ResearchRunRow,
)
from app.quant import (
    costs,
    data,
    details,
    factor_research,
    factors,
    live,
    portfolio,
    quality,
    real,
    research,
    service,
    tasks,
)
from app.quant.service import LabError, LabSettings


async def bind_user_costs(user_id: int = Depends(current_user_id)) -> None:
    """接口中的回测按当前用户的交易成本计算（上下文变量，随 asyncio.to_thread 传入线程）。
    查看别人的公共策略时，少数现算的结果（资金容量、交易明细）另外切换到策略所有者的设置。"""
    costs._ctx_user.set(user_id)


router = APIRouter(prefix="/api/quant", tags=["quant"], dependencies=[Depends(bind_user_costs)])


def _err(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _get(db: Session, sid: int, user_id: int) -> QuantStrategyRow:
    """自己的策略（修改、优化、跟踪等写操作用）。"""
    st = db.get(QuantStrategyRow, sid)
    if st is None or st.user_id != user_id:
        raise HTTPException(404, "策略不存在")
    return st


def _readable(db: Session, sid: int, user_id: int) -> QuantStrategyRow:
    """自己的策略或公共策略（查看、另存、试算用）。"""
    st = db.get(QuantStrategyRow, sid)
    if st is None or (st.user_id != user_id and not st.is_public):
        raise HTTPException(404, "策略不存在")
    return st


def _version(db: Session, vid: int, user_id: int, readable: bool = False) -> QuantVersionRow:
    v = db.get(QuantVersionRow, vid)
    if v is None:
        raise HTTPException(404, "版本不存在")
    (_readable if readable else _get)(db, v.strategy_id, user_id)
    return v


def _own_provider(db: Session, provider_id: int | None, user_id: int) -> None:
    """研究 / 复核模型只能用自己配置的模型供应商（AI 模型资源不在用户之间共用）。"""
    if provider_id is None:
        return
    p = db.get(LLMProviderRow, provider_id)
    if p is None or p.user_id != user_id:
        raise HTTPException(400, "模型供应商不存在（只能使用自己配置的模型）")


def _ready_brief(r: dict | None) -> dict | None:
    return {"status": r.get("status"), "checked_at": r.get("checked_at")} if r else None


def run_dict(r: ResearchRunRow) -> dict:
    return {"id": r.id, "strategy_id": r.strategy_id, "trigger": r.trigger, "status": r.status, "rounds": r.rounds,
            "rounds_done": r.rounds_done, "promoted": r.promoted, "progress": r.progress, "summary": r.summary,
            "error": r.error, "log": r.log, "created_at": r.created_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


def strategy_dict(db: Session, st: QuantStrategyRow, user_id: int | None = None) -> dict:
    """策略摘要；user_id 不是所有者时（查看别人的公共策略）不返回所有者的跟踪与研究模型信息。"""
    mine = user_id is None or st.user_id == user_id
    out = _strategy_dict(db, st)
    out |= {"is_public": bool(st.is_public), "mine": mine}
    if not mine:
        out |= {"provider_id": None, "provider_name": None, "model": None, "follow_account_id": None,
                "readiness": None, "auto_optimize": False}
    return out


def _strategy_dict(db: Session, st: QuantStrategyRow) -> dict:
    champ = db.get(QuantVersionRow, st.champion_id) if st.champion_id else None
    provider = db.get(LLMProviderRow, st.provider_id) if st.provider_id else None
    last_run = db.scalars(select(ResearchRunRow).where(ResearchRunRow.strategy_id == st.id)
                          .order_by(ResearchRunRow.id.desc()).limit(1)).first()
    versions = db.scalars(select(QuantVersionRow.status).where(QuantVersionRow.strategy_id == st.id)).all()
    return {
        "id": st.id, "name": st.name, "style": st.style, "description": st.description, "market": st.market,
        "universe_name": st.universe_name, "benchmark": st.benchmark, "provider_id": st.provider_id,
        "provider_name": provider.name if provider else None, "model": st.model, "settings": st.settings,
        "auto_optimize": st.auto_optimize, "status": st.status, "follow_account_id": st.follow_account_id,
        "readiness": _ready_brief(st.readiness),
        "champion": service.version_brief(champ) if champ else None,
        "champion_curves": champ.curves if champ else None,
        "versions": len(versions), "promotions": sum(1 for x in versions if x in ("champion", "retired")),
        # 按尝试次数折算的夏普：扣除「试了很多次挑最好」的影响后，冠军真实夏普大于 0 的概率
        "dsr": service.dsr_of(db, st, champ) if champ else None,
        "last_run": run_dict(last_run) if last_run else None, "created_at": st.created_at.isoformat(),
        "origin": st.origin or None,
    }


# ---------------------------------------------------------------- 数据与因子


@router.get("/factors")
def factor_catalog() -> list[dict]:
    return factors.catalog()


@router.get("/spec-options")
def spec_options() -> dict:
    """规则编辑器的可选项：申万一级行业。"""
    from app.quant.spec import SW_L1

    return {"industries": list(SW_L1)}


@router.get("/factor-research")
async def factor_scorecard(universe: str = service.DEFAULT_UNIVERSE, window: str = "dev",
                           strategy_id: int | None = None, user_id: int = Depends(current_user_id)) -> dict:
    """因子成绩单：开发期（默认 2018～2024）或留出期，股票池全部可选股票的横截面 IC 与五分组收益。"""
    if window not in ("dev", "holdout"):
        raise HTTPException(400, "window 只能是 dev 或 holdout")

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            extra = []
            cfg = LabSettings()
            uni = universe
            if strategy_id:
                st = _readable(s, strategy_id, user_id)
                cfg, uni = service.settings_of(st), st.universe_name
                champ = s.get(QuantVersionRow, st.champion_id) if st.champion_id else None
                if champ:
                    extra = [(f["name"], f.get("params") or {}) for f in champ.spec.get("factors", [])]
            panel = data.get_panel(s, uni, cfg.train_start, date.today())
            start, end = (cfg.train_start, cfg.valid_end) if window == "dev" else (cfg.holdout_start, panel.dates[-1])
            rows = factor_research.scorecard(panel, start, end, factor_research.default_factors(extra))
            return {"universe": uni, "window": window, "start": start.isoformat(), "end": end.isoformat(),
                    "horizon": factor_research.HORIZON, "step": factor_research.STEP, "factors": rows}
    return await asyncio.to_thread(work)


@router.get("/seeds")
def seeds() -> list[dict]:
    return [{"style": x["style"], "title": x["title"], "description": x["description"], "spec": x["spec"]}
            for x in service.load_seeds()]


class CostsIn(BaseModel):
    commission_rate: float = Field(ge=0, le=0.003)
    commission_min: float = Field(ge=0, le=50)
    dividend_tax: bool = True


def _costs_dict(db: Session, user_id: int) -> dict:
    cur = costs.for_user(user_id)
    return {"costs": cur.to_dict(), "describe": cur.describe(),
            "up_to_date": costs.evaluated_signature(db, user_id) == cur.signature(),
            "task": tasks.status.get(tasks.reevaluate_task(user_id))}


@router.get("/costs")
def get_costs(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """当前用户的交易成本设置（佣金、红利税），以及按该设置重新回测的状态。"""
    return _costs_dict(db, user_id)


@router.put("/costs")
async def put_costs(body: CostsIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """保存当前用户的交易成本：同步到该用户运行中的跟踪赛道，并在后台重新回测该用户在用的策略。"""
    new = costs.TradeCosts(**body.model_dump())
    try:
        new.validate()
    except costs.CostError as e:
        raise _err(e) from None
    changed = new != costs.for_user(user_id)
    synced = costs.save(db, new, user_id)
    db.commit()
    if changed or costs.evaluated_signature(db, user_id) != new.signature():
        tasks.spawn_reevaluate(uid=user_id)
    return _costs_dict(db, user_id) | {"synced_tracks": synced}


@router.post("/reevaluate")
async def reevaluate_all(user_id: int = Depends(current_user_id)) -> dict:
    """按交易成本与最新数据重新回测自己在用的策略（管理员：所有用户的策略，数据修复后使用）。"""
    admin = auth_core.is_admin(user_id)
    if not tasks.spawn_reevaluate(uid=None if admin else user_id):
        raise HTTPException(409, "重新回测正在进行中")
    return {"task": tasks.status.get(tasks.reevaluate_task(None if admin else user_id))}


@router.get("/data-quality")
def data_quality(db: Session = Depends(get_db), _: int = Depends(require_admin)) -> dict:
    """最近一次数据质量扫描结果（每晚数据更新后自动扫描）。管理员。"""
    return quality.report(db) | {"task": tasks.status.get(tasks.QUALITY),
                                 "reevaluate_task": tasks.status.get(tasks.REEVALUATE)}


@router.post("/data-quality/scan")
async def data_quality_scan(_: int = Depends(require_admin)) -> dict:
    if not tasks.spawn_quality():
        raise HTTPException(409, "扫描正在进行中")
    return {"task": tasks.status.get(tasks.QUALITY)}


class IgnoreIn(BaseModel):
    key: str = Field(max_length=80)
    ignore: bool = True


@router.post("/data-quality/ignore")
def data_quality_ignore(body: IgnoreIn, db: Session = Depends(get_db), _: int = Depends(require_admin)) -> dict:
    """把确认无误的异常标记为忽略（之后不再推送、页面上折叠显示）。"""
    quality.set_ignored(db, body.key, body.ignore)
    db.commit()
    return {"ok": True}


class RepairIn(BaseModel):
    symbol: str = Field(max_length=20)


@router.post("/data-quality/repair")
async def data_quality_repair(body: RepairIn, _: int = Depends(require_admin)) -> dict:
    """重新下载该标的的日线与除权数据，然后在后台重新回测所有在用策略并重新扫描。"""
    def work() -> str:
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            msg = quality.repair(s, body.symbol)
            s.commit()
            return msg

    try:
        msg = await asyncio.to_thread(work)
    except ValueError as e:
        raise _err(e) from None
    tasks.spawn_reevaluate(rescan=True)
    return {"message": msg, "task": tasks.status.get(tasks.REEVALUATE)}


@router.get("/data")
def data_status(universe: str = service.DEFAULT_UNIVERSE, db: Session = Depends(get_db)) -> dict:
    try:
        cov = data.coverage(db, universe)
    except Exception as e:  # 股票池成分需要联网解析，失败时只返回任务状态
        cov = {"error": str(e)}
    out = {"universe": universe, "coverage": cov, "task": tasks.status.get(f"prepare:{universe}"),
           "default_start": data.data_start(LabSettings().train_start).isoformat()}
    if universe == service.DEFAULT_UNIVERSE and not cov.get("ready"):
        # 完整数据还没准备好时，告诉页面快速体验数据的情况（快速开始见 /data/quick-start）
        quick = service.QUICK_UNIVERSE
        try:
            qcov = data.coverage(db, quick)
        except Exception as e:  # noqa: BLE001
            qcov = {"error": str(e)}
        out["quick"] = {"universe": quick, "coverage": qcov, "task": tasks.status.get(f"prepare:{quick}")}
    return out


class PrepareIn(BaseModel):
    universe: str = service.DEFAULT_UNIVERSE
    train_start: date = LabSettings().train_start


@router.post("/data/prepare")
async def prepare(body: PrepareIn, _: int = Depends(require_admin)) -> dict:
    if not tasks.spawn_prepare(body.universe, body.train_start):
        raise HTTPException(409, "数据准备正在进行")
    return {"ok": True}


@router.post("/data/quick-start")
async def quick_start(_: int = Depends(require_admin)) -> dict:
    """快速开始：几分钟准备好快速体验股票池，随后自动在后台准备完整数据。"""
    if tasks.busy(f"prepare:{service.DEFAULT_UNIVERSE}") or not tasks.spawn_quick_start(LabSettings().train_start):
        raise HTTPException(409, "数据准备正在进行")
    return {"ok": True}


# ---------------------------------------------------------------- 策略


@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """自己在用的策略与公共策略（mine 区分）。"""
    rows = db.scalars(select(QuantStrategyRow).where(
        (QuantStrategyRow.user_id == user_id) | QuantStrategyRow.is_public.is_(True),
        QuantStrategyRow.status == "active").order_by(QuantStrategyRow.id))
    return {"strategies": [strategy_dict(db, st, user_id) for st in rows], "seed_task": tasks.status.get("seed")}


class SeedIn(BaseModel):
    provider_id: int | None = None
    model: str | None = None
    styles: list[str] | None = None


@router.post("/strategies/seed")
async def seed(body: SeedIn, user_id: int = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """创建风格策略库（管理员；创建的策略为公共策略）。"""
    _own_provider(db, body.provider_id, user_id)
    if not tasks.spawn_seed(body.provider_id, body.model, body.styles):
        raise HTTPException(409, "正在创建中")
    return {"ok": True}


class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    style: str = "custom"
    description: str = ""
    spec: dict
    universe_name: str = service.DEFAULT_UNIVERSE
    provider_id: int | None = None
    model: str | None = None
    settings: dict = Field(default_factory=dict)
    auto_optimize: bool = False
    # 新建前在对话框里试算过的规则的开发期夏普（每次试算一个；没有夏普时为 null），计入测试次数
    trial_scores: list[float | None] = Field(default_factory=list, max_length=service.MAX_TRIALS)


@router.post("/strategies")
async def create(body: StrategyIn, user_id: int = Depends(current_user_id), db: Session = Depends(get_db)) -> dict:
    """新建自己的（私有）策略。"""
    if body.auto_optimize and body.provider_id is None:
        raise HTTPException(400, "开启每晚自动优化需要先选择研究模型")
    _own_provider(db, body.provider_id, user_id)

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            st = service.create_strategy(s, **body.model_dump(), user_id=user_id)
            s.commit()
            return strategy_dict(s, st, user_id)
    try:
        return await asyncio.to_thread(work)
    except (LabError, ValueError) as e:
        raise _err(e) from None


class DraftIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    provider_id: int
    model: str | None = None
    base_spec: dict | None = None


@router.post("/strategies/draft")
async def draft_strategy(body: DraftIn, user_id: int = Depends(current_user_id), db: Session = Depends(get_db)) -> dict:
    """一句话创建策略：用自己配置的模型把中文描述翻译成规则（只生成，不回测；描述里做不到的条件会列出来）。"""
    from app.quant import draft

    _own_provider(db, body.provider_id, user_id)
    db.close()
    try:
        return await draft.draft(body.provider_id, body.model, body.text, body.base_spec)
    except LabError as e:
        raise _err(e) from None


class PreviewIn(BaseModel):
    spec: dict
    universe_name: str = service.DEFAULT_UNIVERSE
    strategy_id: int | None = None         # 给出时沿用该策略的股票池与检验区间


@router.post("/preview")
async def preview(body: PreviewIn, user_id: int = Depends(current_user_id)) -> dict:
    """试算规则：只回测开发期，不保存、不返回留出期结果。"""
    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            uni, settings = body.universe_name, None
            if body.strategy_id:
                st = _readable(s, body.strategy_id, user_id)
                uni, settings = st.universe_name, st.settings
            return service.preview(s, body.spec, uni, settings)
    try:
        return await asyncio.to_thread(work)
    except (LabError, ValueError) as e:
        raise _err(e) from None


class ForkIn(BaseModel):
    version_id: int | None = None          # 默认为当前冠军版本
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    copy_notes: bool = True
    provider_id: int | None = None
    model: str | None = None
    auto_optimize: bool = False


@router.post("/strategies/{sid}/fork")
async def fork(sid: int, body: ForkIn, user_id: int = Depends(current_user_id)) -> dict:
    """另存为新策略：以某个版本为初始版本建立独立的策略。"""
    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            src = _readable(s, sid, user_id)            # 可以另存别人的公共策略
            v = _version(s, body.version_id, user_id, readable=True) if body.version_id else (
                s.get(QuantVersionRow, src.champion_id) if src.champion_id else None)
            if v is None:
                raise LabError("来源策略没有可用的版本")
            if body.auto_optimize and body.provider_id is None:
                raise LabError("开启每晚自动优化需要先选择研究模型")
            _own_provider(s, body.provider_id, user_id)
            st = service.fork_strategy(s, src, v, name=body.name.strip(), description=body.description,
                                       copy_notes=body.copy_notes, provider_id=body.provider_id,
                                       model=body.model, auto_optimize=body.auto_optimize, user_id=user_id)
            s.commit()
            return strategy_dict(s, st, user_id)
    try:
        return await asyncio.to_thread(work)
    except (LabError, ValueError) as e:
        raise _err(e) from None


@router.get("/strategies/{sid}")
def get_strategy(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    st = _readable(db, sid, user_id)
    versions = db.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == sid)
                          .order_by(QuantVersionRow.number.desc()))
    runs = db.scalars(select(ResearchRunRow).where(ResearchRunRow.strategy_id == sid)
                      .order_by(ResearchRunRow.id.desc()).limit(20))
    champ = db.get(QuantVersionRow, st.champion_id) if st.champion_id else None
    return strategy_dict(db, st, user_id) | {
        "champion_detail": service.version_detail(champ) if champ else None,
        "quality": service.research_quality(db, st),
        "version_list": [service.version_brief(v) for v in versions],
        "runs": [run_dict(r) for r in runs],
        "settings_full": service.settings_of(st).to_dict(),
        "research_notes": st.research_notes or [],
    }


@router.get("/strategies/{sid}/timeline")
def strategy_timeline(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """AI 优化时间线：每一轮的诊断、每个候选改了什么、检验结果与是否采纳（只含开发期）。"""
    from app.quant import timeline

    return timeline.build(db, _readable(db, sid, user_id))


@router.get("/strategies/{sid}/report", response_class=HTMLResponse)
def strategy_report(sid: int, download: bool = False, db: Session = Depends(get_db),
                    user_id: int = Depends(current_user_id)) -> HTMLResponse:
    """策略体检报告（独立 HTML，可离线打开）。可实盘检查只在所有者导出时包含。"""
    from urllib.parse import quote

    from app.quant import report

    st = _readable(db, sid, user_id)
    try:
        page = report.render(db, st, include_private=st.user_id == user_id)
    except LabError as e:
        raise _err(e) from None
    name = quote(f"{st.name}-体检报告.html")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"} if download else {}
    return HTMLResponse(page, headers=headers)


@router.get("/strategies/{sid}/export")
def export_strategy(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> Response:
    """导出策略文件（YAML）：冠军规则、股票池、检验设置、来源与测试历史。公共策略也可以导出。"""
    from urllib.parse import quote

    from app.quant import exchange

    st = _readable(db, sid, user_id)
    try:
        text = exchange.dumps(exchange.export(db, st))
    except LabError as e:
        raise _err(e) from None
    return Response(text, media_type="application/x-yaml; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(exchange.filename(st))}"})


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)
    name: str | None = Field(None, max_length=64)
    universe: str | None = None


@router.post("/strategies/import")
async def import_strategy(body: ImportIn, user_id: int = Depends(current_user_id)) -> dict:
    """导入策略文件：新建自己的私有策略并回测，测试历史从文件继承。"""
    from app.quant import exchange

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            st, warnings = exchange.import_strategy(s, body.text, user_id, body.name, body.universe)
            s.commit()
            return strategy_dict(s, st, user_id) | {"warnings": warnings}
    try:
        return await asyncio.to_thread(work)
    except (LabError, ValueError) as e:
        raise _err(e) from None


class StrategyPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    description: str | None = None
    provider_id: int | None = None
    model: str | None = None
    settings: dict | None = None
    auto_optimize: bool | None = None
    status: str | None = Field(None, pattern="^(active|archived)$")
    research_notes: list[str] | None = None
    is_public: bool | None = None          # 只有管理员可以把自己的策略设为公共


@router.patch("/strategies/{sid}")
def patch_strategy(sid: int, body: StrategyPatch, db: Session = Depends(get_db),
                   user_id: int = Depends(current_user_id)) -> dict:
    st = _get(db, sid, user_id)
    data_ = body.model_dump(exclude_unset=True)
    if "provider_id" in data_:
        _own_provider(db, data_["provider_id"], user_id)
    if data_.get("is_public") is not None:
        if not auth_core.is_admin(user_id):
            raise HTTPException(403, "只有管理员可以设置公共策略")
        st.is_public = data_["is_public"]
    if "settings" in data_:
        try:
            # 修改检验区间后旧版本的指标不再可比：只允许修改门槛类设置，区间在新建策略时确定
            old = service.settings_of(st)
            new = LabSettings.from_dict({**st.settings, **(data_["settings"] or {})})
            for k in ("train_start", "train_end", "valid_start", "valid_end", "holdout_start", "initial_cash"):
                if getattr(new, k) != getattr(old, k):
                    raise LabError("检验区间与本金创建后不能修改（否则各版本的指标不可比），请新建策略")
            st.settings = new.to_dict()
        except LabError as e:
            raise _err(e) from None
    for k in ("name", "description", "provider_id", "model", "auto_optimize", "status"):
        if k in data_:
            setattr(st, k, data_[k])
    if data_.get("research_notes") is not None:
        # 用户编辑后的笔记：保留原有条目的来源信息，新增的记为手动
        old = {n["text"]: n for n in (st.research_notes or [])}
        st.research_notes = [old.get(t.strip()) or {"text": t.strip()[:150], "source": "manual",
                                                     "at": date.today().isoformat()}
                             for t in data_["research_notes"] if t.strip()][-research.NOTES_KEEP:]
    db.commit()
    return strategy_dict(db, st, user_id)


# ---------------------------------------------------------------- 版本


@router.get("/versions/{vid}")
def get_version(vid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return service.version_detail(_version(db, vid, user_id, readable=True))


@router.get("/versions/{vid}/trades")
async def version_trades(vid: int, window: str = "holdout", user_id: int = Depends(current_user_id)) -> dict:
    """版本的交易明细：调仓记录与选股理由、每笔持有收益、标的汇总、成交流水。"""
    if window not in details.WINDOWS:
        raise HTTPException(400, f"window 只能是 {', '.join(details.WINDOWS)}")

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            v = _version(s, vid, user_id, readable=True)
            st = _readable(s, v.strategy_id, user_id)
            with costs.use(st.user_id):        # 公共策略按所有者的交易成本，与保存的回测结果一致
                return details.details(v.id, v.spec, service.panel_for(s, st), service.settings_of(st), window)
    return await asyncio.to_thread(work)


class ManualIn(BaseModel):
    spec: dict
    title: str = ""
    rationale: str = ""


@router.post("/strategies/{sid}/versions")
async def manual_version(sid: int, body: ManualIn, user_id: int = Depends(current_user_id)) -> dict:
    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            st = _get(s, sid, user_id)
            v = research.evaluate_manual(s, st, body.spec, body.title, body.rationale)
            s.commit()
            return service.version_detail(v)
    try:
        return await asyncio.to_thread(work)
    except (LabError, ValueError) as e:
        raise _err(e) from None


@router.post("/versions/{vid}/promote")
def promote(vid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    v = _version(db, vid, user_id)
    st = _get(db, v.strategy_id, user_id)
    service.promote(db, st, v, "手动设为冠军")
    db.commit()
    return service.version_brief(v)


# ---------------------------------------------------------------- 研究


class ResearchIn(BaseModel):
    rounds: int | None = Field(None, ge=1, le=50)


@router.post("/strategies/{sid}/research")
async def start_research(sid: int, body: ResearchIn, db: Session = Depends(get_db),
                         user_id: int = Depends(current_user_id)) -> dict:
    st = _get(db, sid, user_id)
    try:
        run = research.start_run(db, st, body.rounds)
    except LabError as e:
        raise _err(e) from None
    db.commit()
    tasks.spawn_research(run.id)
    return run_dict(run)


@router.get("/runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    r = db.get(ResearchRunRow, rid)
    if r is None:
        raise HTTPException(404, "研究记录不存在")
    _readable(db, r.strategy_id, user_id)
    return run_dict(r)


@router.post("/runs/{rid}/cancel")
def cancel_run(rid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    r = db.get(ResearchRunRow, rid)
    if r is None:
        raise HTTPException(404, "研究记录不存在")
    _get(db, r.strategy_id, user_id)
    if r.status == "pending":
        r.status, r.progress = "cancelled", "已取消"
        db.commit()
    research.cancel(rid)
    return {"ok": True}


# ---------------------------------------------------------------- 每日信号与实时跟踪


@router.get("/strategies/{sid}/signal")
async def get_signal(sid: int, capital: float | None = None, user_id: int = Depends(current_user_id)) -> dict:
    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            st = _readable(s, sid, user_id)       # 公共策略的今日信号也可以查看（跟踪需先另存）
            if not st.champion_id:
                raise LabError("策略还没有冠军版本")
            return live.signal(s, st, capital)
    try:
        return await asyncio.to_thread(work)
    except (LabError, ValueError) as e:
        raise _err(e) from None


class FollowIn(BaseModel):
    initial_cash: float = Field(1_000_000, gt=0)


@router.post("/strategies/{sid}/follow")
def follow(sid: int, body: FollowIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from decimal import Decimal

    from app.arena.lifecycle import ArenaError

    st = _get(db, sid, user_id)
    try:
        acc = live.follow(db, st, user_id, Decimal(str(body.initial_cash)))
    except ArenaError as e:
        raise _err(e) from None
    db.commit()
    return {"account_id": acc.id, "track_id": acc.track_id}


def tracking_of(db: Session, account_id: int | None) -> dict | None:
    """实时跟踪账户的表现、持仓、最近决策与调仓清单（策略与组合共用）。"""
    from app.arena import orders, queries
    from app.models import AccountRow, DecisionRow, InstrumentRow, NavSnapshotRow, OrderRow, RoundRow, TrackRow

    if not account_id:
        return None
    acc = db.get(AccountRow, account_id)
    track = db.get(TrackRow, acc.track_id) if acc else None
    if acc is None or track is None:
        return None
    navs = db.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav, NavSnapshotRow.benchmark_close)
                      .where(NavSnapshotRow.account_id == acc.id).order_by(NavSnapshotRow.date)).all()
    last = db.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                      .where(DecisionRow.account_id == acc.id).order_by(DecisionRow.id.desc()).limit(1)).first()
    pending = orders.open_orders(db, acc.id)
    recent = list(db.scalars(select(OrderRow).where(OrderRow.decision_id == last[0].id)
                             .order_by(OrderRow.id))) if last else []
    names = {}
    for o in [*pending, *recent]:
        if o.symbol not in names:
            inst = db.get(InstrumentRow, o.symbol)
            names[o.symbol] = inst.name if inst else o.symbol
    nav0 = float(track.initial_cash)
    b0 = next((float(b) for _, _, b in navs if b is not None), None)
    return {
        "account_id": acc.id, "track_id": track.id, "track_status": track.status,
        "start_date": str(track.start_date) if track.start_date else None, "initial_cash": nav0,
        "nav": [{"date": str(d), "nav": float(n) / nav0,
                 "bench": float(b) / b0 if b is not None and b0 else None} for d, n, b in navs],
        "positions": queries.positions(db, acc, track),
        "last_decision": {"data_date": str(last[1].data_date), "fill_date": str(last[1].fill_date),
                          "analysis": last[0].analysis, "parsed": last[0].parsed} if last else None,
        "orders": [queries.order_dict(o) | {"name": names.get(o.symbol, o.symbol)} for o in recent],
        "open_orders": [queries.order_dict(o) | {"name": names.get(o.symbol, o.symbol)} for o in pending],
    }


@router.get("/strategies/{sid}/tracking")
def tracking(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict | None:
    """实时跟踪账户的表现、持仓、最近决策与待成交订单（调仓清单）。"""
    return tracking_of(db, _get(db, sid, user_id).follow_account_id)


# ---------------------------------------------------------------- 多风格组合


def _pf(db: Session, pid: int, user_id: int) -> QuantPortfolioRow:
    pf = db.get(QuantPortfolioRow, pid)
    if pf is None or pf.user_id != user_id or pf.status != "active":
        raise HTTPException(404, "组合不存在")
    return pf


def pf_dict(db: Session, pf: QuantPortfolioRow) -> dict:
    members = []
    for m in pf.members:
        st = db.get(QuantStrategyRow, m["strategy_id"])
        members.append({"strategy_id": m["strategy_id"], "weight": m["weight"], "name": st.name if st else "?"})
    return {"id": pf.id, "name": pf.name, "description": pf.description, "members": members,
            "follow_account_id": pf.follow_account_id, "created_at": pf.created_at.isoformat(),
            "readiness": _ready_brief(pf.readiness), "settings": pf.settings or {}}


class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    members: list[dict]
    description: str = ""
    settings: dict | None = None


def _pf_settings(settings: dict | None) -> dict:
    """校验组合设置（目前只有资金配置 alloc）。"""
    from app.quant.allocation import AllocError, AllocSettings

    try:
        alloc = AllocSettings.from_dict((settings or {}).get("alloc"))
    except (AllocError, TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    return {"alloc": alloc.to_dict()}


@router.get("/portfolios")
def list_portfolios(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    rows = db.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.user_id == user_id,
                                                      QuantPortfolioRow.status == "active")
                      .order_by(QuantPortfolioRow.id))
    return [pf_dict(db, pf) for pf in rows]


@router.post("/portfolios")
def create_portfolio(body: PortfolioIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    try:
        members = portfolio.validate_members(db, body.members, user_id)
    except (LabError, KeyError, ValueError) as e:
        raise _err(e) from None
    pf = QuantPortfolioRow(user_id=user_id, name=body.name, members=members, description=body.description,
                           settings=_pf_settings(body.settings))
    db.add(pf)
    db.commit()
    return pf_dict(db, pf)


@router.patch("/portfolios/{pid}")
def patch_portfolio(pid: int, body: PortfolioIn, db: Session = Depends(get_db),
                    user_id: int = Depends(current_user_id)) -> dict:
    pf = _pf(db, pid, user_id)
    try:
        pf.members = portfolio.validate_members(db, body.members, user_id)
    except (LabError, KeyError, ValueError) as e:
        raise _err(e) from None
    pf.name, pf.description = body.name, body.description
    if body.settings is not None:
        pf.settings = _pf_settings(body.settings)
    db.commit()
    return pf_dict(db, pf)


@router.delete("/portfolios/{pid}")
def delete_portfolio(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    pf = _pf(db, pid, user_id)
    pf.status = "archived"
    db.commit()
    return {"ok": True}


@router.get("/portfolios/{pid}")
async def get_portfolio(pid: int, user_id: int = Depends(current_user_id)) -> dict:
    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            pf = _pf(s, pid, user_id)
            return pf_dict(s, pf) | {"report": portfolio.evaluate_portfolio(s, pf)}
    try:
        return await asyncio.to_thread(work)
    except LabError as e:
        raise _err(e) from None


@router.get("/portfolios/{pid}/signal")
async def portfolio_signal(pid: int, capital: float | None = None, user_id: int = Depends(current_user_id)) -> dict:
    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            return portfolio.signal(s, _pf(s, pid, user_id), capital)
    return await asyncio.to_thread(work)


@router.post("/portfolios/{pid}/follow")
def follow_portfolio(pid: int, body: FollowIn, db: Session = Depends(get_db),
                     user_id: int = Depends(current_user_id)) -> dict:
    from decimal import Decimal

    from app.arena.lifecycle import ArenaError

    pf = _pf(db, pid, user_id)
    try:
        acc = live.follow_portfolio(db, pf, user_id, Decimal(str(body.initial_cash)))
    except ArenaError as e:
        raise _err(e) from None
    db.commit()
    return {"account_id": acc.id, "track_id": acc.track_id}


@router.get("/portfolios/{pid}/tracking")
def portfolio_tracking(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict | None:
    return tracking_of(db, _pf(db, pid, user_id).follow_account_id)



# ---------------------------------------------------------------- v1.7：对账、健康度、资金容量、回放校验


def _own_account(db: Session, account_id: int, user_id: int):
    from app.models import AccountRow, TrackRow

    acc = db.get(AccountRow, account_id)
    track = db.get(TrackRow, acc.track_id) if acc else None
    if acc is None or track is None or track.user_id != user_id:
        raise HTTPException(404, "账户不存在")
    return acc


@router.get("/accounts/{account_id}/check")
async def account_check(account_id: int, user_id: int = Depends(current_user_id)) -> dict:
    """跟踪账户的回测—跟踪对账与健康度。"""
    from app.quant import reconcile

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            _own_account(s, account_id, user_id)
            recon = reconcile.reconcile(s, account_id)
            return {"reconcile": recon, "health": reconcile.health(s, account_id, recon)}
    return await asyncio.to_thread(work)


@router.get("/strategies/{sid}/capacity")
async def strategy_capacity(sid: int, user_id: int = Depends(current_user_id)) -> dict:
    from app.quant import capacity

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            st = _readable(s, sid, user_id)
            v = s.get(QuantVersionRow, st.champion_id)
            from app.quant.spec import parse_spec

            with costs.use(st.user_id):        # 公共策略按所有者的交易成本
                return capacity.capacity([(parse_spec(v.spec), 1.0)], service.panel_for(s, st),
                                         service.settings_of(st), key=("v", v.id)) | {"version": v.number}
    return await asyncio.to_thread(work)


@router.get("/portfolios/{pid}/capacity")
async def portfolio_capacity(pid: int, user_id: int = Depends(current_user_id)) -> dict:
    from app.quant import capacity

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            pf = _pf(s, pid, user_id)
            members = portfolio.members_of(s, pf)
            st0 = members[0].strategy
            alloc = portfolio.alloc_of(pf)
            return capacity.capacity([(m.spec, m.weight) for m in members], service.panel_for(s, st0),
                                     service.settings_of(st0),
                                     key=("p", pf.id, tuple((m.version.id, m.weight) for m in members),
                                          str(alloc.to_dict())), alloc=alloc)
    return await asyncio.to_thread(work)


class ReplayIn(BaseModel):
    start: date | None = None
    initial_cash: float = Field(1_000_000, gt=0)


def _replay_list(db: Session, agent_filter) -> list[dict]:
    from app.arena import tasks as arena_tasks
    from app.models import AccountRow, AgentRow, TrackRow

    out = []
    agents = [a.id for a in db.scalars(select(AgentRow).where(AgentRow.kind == "quant", agent_filter))]
    if not agents:
        return out
    q = (select(AccountRow, TrackRow).join(TrackRow, AccountRow.track_id == TrackRow.id)
         .where(AccountRow.agent_id.in_(agents), TrackRow.name.like(f"{live.REPLAY_PREFIX}%"))
         .order_by(TrackRow.id.desc()))
    for acc, track in db.execute(q):
        out.append({"track_id": track.id, "account_id": acc.id, "status": track.status,
                    "start_date": str(track.start_date) if track.start_date else None,
                    "replay_until": str(track.replay_until) if track.replay_until else None,
                    "progress": arena_tasks.last_result.get(track.id), "created_at": track.created_at.isoformat()})
    return out


async def _spawn_replay(acc) -> dict:
    from app.arena import tasks as arena_tasks

    arena_tasks.spawn_replay(acc.track_id)
    return {"account_id": acc.id, "track_id": acc.track_id}


@router.post("/strategies/{sid}/replay-check")
async def strategy_replay_check(sid: int, body: ReplayIn, db: Session = Depends(get_db),
                                user_id: int = Depends(current_user_id)) -> dict:
    from decimal import Decimal

    from app.arena.lifecycle import ArenaError

    st = _get(db, sid, user_id)
    start = body.start or service.settings_of(st).holdout_start
    try:
        acc = live.replay_check(db, user_id, strategy=st, start=start, initial_cash=Decimal(str(body.initial_cash)))
    except ArenaError as e:
        raise _err(e) from None
    db.commit()
    return await _spawn_replay(acc)


@router.post("/portfolios/{pid}/replay-check")
async def portfolio_replay_check(pid: int, body: ReplayIn, db: Session = Depends(get_db),
                                 user_id: int = Depends(current_user_id)) -> dict:
    from decimal import Decimal

    from app.arena.lifecycle import ArenaError

    pf = _pf(db, pid, user_id)
    start = body.start or LabSettings().holdout_start
    try:
        acc = live.replay_check(db, user_id, portfolio=pf, start=start, initial_cash=Decimal(str(body.initial_cash)))
    except ArenaError as e:
        raise _err(e) from None
    db.commit()
    return await _spawn_replay(acc)


@router.get("/strategies/{sid}/replay-checks")
def strategy_replay_checks(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list:
    from app.models import AgentRow

    _get(db, sid, user_id)
    return _replay_list(db, AgentRow.quant_strategy_id == sid)


@router.get("/portfolios/{pid}/replay-checks")
def portfolio_replay_checks(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list:
    from app.models import AgentRow

    _pf(db, pid, user_id)
    return _replay_list(db, AgentRow.quant_portfolio_id == pid)



# ---------------------------------------------------------------- 「可实盘」检查（PRD 3.9）


async def _readiness(kind: str, ref_id: int, refresh: bool) -> dict:
    from app.quant import readiness

    model = QuantStrategyRow if kind == "strategy" else QuantPortfolioRow

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            row = s.get(model, ref_id)
            if not refresh and row.readiness:
                return row.readiness
            out = readiness.check(s, kind, ref_id)
        # 先算后写：长计算结束后在短事务中保存
        with get_sessionmaker()() as s:
            s.get(model, ref_id).readiness = out
            s.commit()
        return out
    return await asyncio.to_thread(work)


@router.get("/strategies/{sid}/readiness")
async def strategy_readiness(sid: int, refresh: bool = False, db: Session = Depends(get_db),
                             user_id: int = Depends(current_user_id)) -> dict:
    _get(db, sid, user_id)
    db.close()
    return await _readiness("strategy", sid, refresh)


@router.get("/portfolios/{pid}/readiness")
async def portfolio_readiness(pid: int, refresh: bool = False, db: Session = Depends(get_db),
                              user_id: int = Depends(current_user_id)) -> dict:
    _pf(db, pid, user_id)
    db.close()
    return await _readiness("portfolio", pid, refresh)


# ---------------------------------------------------------------- AI 交易复核（规则 + AI 复核，两个账户并行）


class ReviewIn(BaseModel):
    provider_id: int
    model: str | None = Field(None, max_length=255)


@router.get("/portfolios/{pid}/review")
async def portfolio_review(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """规则账户与 AI 复核账户的对比、每次复核的结论与被否决股票之后的表现。"""
    from app.quant import review

    _pf(db, pid, user_id)
    db.close()

    def work() -> dict:
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            return review.report(s, s.get(QuantPortfolioRow, pid))

    return await asyncio.to_thread(work)


@router.get("/portfolios/{pid}/review/evaluation")
async def portfolio_review_evaluation(pid: int, db: Session = Depends(get_db),
                                      user_id: int = Depends(current_user_id)) -> dict:
    """AI 复核有效性评估（预注册 docs/prereg/2026-10-08-ai-review.md）：评估时点之前只有进度。"""
    from app.quant import review_eval

    pf = _pf(db, pid, user_id)
    # 预注册写死了组合与账户编号：只有编号都对得上（就是当时开启评估的那两个组合）才适用；
    # 其他部署里同编号的组合是别的组合，不显示评估
    ai_id = ((pf.settings or {}).get("review") or {}).get("account_id")
    applies = (pid, pf.follow_account_id, ai_id) in set(review_eval.PAIRS)
    db.close()
    if not applies:
        return {"applies": False}

    def work() -> dict:
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            return {"applies": True, **review_eval.evaluate(s)}

    return await asyncio.to_thread(work)


@router.post("/portfolios/{pid}/review")
def enable_portfolio_review(pid: int, body: ReviewIn, db: Session = Depends(get_db),
                            user_id: int = Depends(current_user_id)) -> dict:
    """开启 AI 复核：在规则账户所在的实时跟踪赛道中加入 AI 复核账户（下一个收盘后开始决策）。"""
    from app.arena.lifecycle import ArenaError
    from app.models import LLMProviderRow
    from app.quant import review

    pf = _pf(db, pid, user_id)
    _own_provider(db, body.provider_id, user_id)
    try:
        acc = review.enable(db, pf, body.provider_id, body.model)
    except ArenaError as e:
        raise _err(e) from None
    from app.llm.base import is_local_endpoint
    from app.models import AgentRow

    provider = db.get(LLMProviderRow, body.provider_id)
    agent = db.get(AgentRow, acc.agent_id)

    if provider is not None and is_local_endpoint(provider.base_url):
        # 本地模型关闭思考过程：复核按批进行，避免收盘后耗时过长
        agent.params = {**(agent.params or {}), "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    db.commit()
    return {"account_id": acc.id, "review": review.settings(pf)}


@router.delete("/portfolios/{pid}/review")
def disable_portfolio_review(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.quant import review

    pf = _pf(db, pid, user_id)
    review.disable(db, pf)
    db.commit()
    return {"review": review.settings(pf)}


# ---------------------------------------------------------------- 自选倾斜账户


@router.get("/portfolios/{pid}/tilt")
async def portfolio_tilt(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """自选倾斜账户与规则账户的对比、当前自选权重与最近一次调整。"""
    from app.watch import tilt

    _pf(db, pid, user_id)
    db.close()

    def work() -> dict:
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            return tilt.report(s, s.get(QuantPortfolioRow, pid))

    return await asyncio.to_thread(work)


@router.post("/portfolios/{pid}/tilt")
def enable_portfolio_tilt(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.arena.lifecycle import ArenaError
    from app.watch import tilt

    pf = _pf(db, pid, user_id)
    try:
        acc = tilt.enable(db, pf)
    except ArenaError as e:
        raise _err(e) from None
    db.commit()
    return {"account_id": acc.id, "tilt": tilt.settings(pf)}


@router.delete("/portfolios/{pid}/tilt")
def disable_portfolio_tilt(pid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.watch import tilt

    pf = _pf(db, pid, user_id)
    tilt.disable(db, pf)
    db.commit()
    return {"tilt": tilt.settings(pf)}


# ---------------------------------------------------------------- 实盘对照（PRD 3.9 G8）


class RealAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["strategy", "portfolio"]
    ref_id: int
    initial_cash: float = Field(gt=0)
    start_date: date
    cash_flows: list[dict] = Field(default_factory=list)
    note: str = ""


class RealFillIn(BaseModel):
    trade_date: date
    symbol: str = Field(min_length=6, max_length=20)
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    fee: float = Field(0, ge=0)
    name: str = ""


class RealImportIn(BaseModel):
    content_base64: str = Field(min_length=1, max_length=20_000_000)
    commit: bool = False


def _real(db: Session, rid: int, user_id: int) -> RealAccountRow:
    acc = db.get(RealAccountRow, rid)
    if acc is None or acc.user_id != user_id or acc.status != "active":
        raise HTTPException(404, "实盘账户不存在")
    return acc


def _check_ref(db: Session, body: RealAccountIn, user_id: int) -> None:
    if body.kind == "strategy":
        _get(db, body.ref_id, user_id)
    else:
        _pf(db, body.ref_id, user_id)
    for c in body.cash_flows:
        if real.parse_date(c.get("date")) is None or not isinstance(c.get("amount"), (int, float)):
            raise HTTPException(400, "资金变动需要日期与金额")


@router.get("/real-accounts")
def list_real_accounts(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    rows = db.scalars(select(RealAccountRow).where(RealAccountRow.user_id == user_id,
                                                   RealAccountRow.status == "active").order_by(RealAccountRow.id))
    return [real.account_dict(db, a) for a in rows]


@router.post("/real-accounts")
def create_real_account(body: RealAccountIn, db: Session = Depends(get_db),
                        user_id: int = Depends(current_user_id)) -> dict:
    _check_ref(db, body, user_id)
    acc = RealAccountRow(user_id=user_id, **body.model_dump())
    db.add(acc)
    db.commit()
    return real.account_dict(db, acc)


@router.patch("/real-accounts/{rid}")
def patch_real_account(rid: int, body: RealAccountIn, db: Session = Depends(get_db),
                       user_id: int = Depends(current_user_id)) -> dict:
    acc = _real(db, rid, user_id)
    _check_ref(db, body, user_id)
    for k, v in body.model_dump().items():
        setattr(acc, k, v)
    db.commit()
    return real.account_dict(db, acc)


@router.delete("/real-accounts/{rid}")
def delete_real_account(rid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    acc = _real(db, rid, user_id)
    acc.status = "archived"          # 归档而不是删除：成交记录保留
    db.commit()
    return {"ok": True}


@router.get("/real-accounts/{rid}/fills")
def list_real_fills(rid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    acc = _real(db, rid, user_id)
    rows = db.scalars(select(RealFillRow).where(RealFillRow.real_account_id == acc.id)
                      .order_by(RealFillRow.trade_date.desc(), RealFillRow.id.desc()))
    return [real.fill_dict(f) for f in rows]


@router.post("/real-accounts/{rid}/fills")
def add_real_fill(rid: int, body: RealFillIn, db: Session = Depends(get_db),
                  user_id: int = Depends(current_user_id)) -> dict:
    acc = _real(db, rid, user_id)
    sym = real.normalize_symbol(body.symbol)
    if sym is None:
        raise HTTPException(400, f"无法识别代码 {body.symbol}")
    out = real.add_fills(db, acc, [real.ParsedFill(body.trade_date, sym, body.name, body.side, body.qty,
                                                   body.price, body.fee)], "manual")
    db.commit()
    if not out["added"]:
        raise HTTPException(400, "已有完全相同的成交记录")
    return out


@router.delete("/real-accounts/{rid}/fills/{fid}")
def delete_real_fill(rid: int, fid: int, db: Session = Depends(get_db),
                     user_id: int = Depends(current_user_id)) -> dict:
    acc = _real(db, rid, user_id)
    f = db.get(RealFillRow, fid)
    if f is None or f.real_account_id != acc.id:
        raise HTTPException(404, "成交记录不存在")
    db.delete(f)
    db.commit()
    return {"ok": True}


@router.post("/real-accounts/{rid}/import")
def import_real_fills(rid: int, body: RealImportIn, db: Session = Depends(get_db),
                      user_id: int = Depends(current_user_id)) -> dict:
    """导入券商导出的成交明细（CSV）。commit=false 只解析预览，commit=true 写入。"""
    import base64
    import binascii

    acc = _real(db, rid, user_id)
    try:
        parsed = real.parse_fills(real.decode(base64.b64decode(body.content_base64)))
    except (binascii.Error, real.RealError) as e:
        raise _err(e) from None
    out = {"rows": [r.as_dict() for r in parsed["rows"]], "skipped": parsed["skipped"],
           "columns": parsed["columns"]}
    if body.commit:
        out |= real.add_fills(db, acc, parsed["rows"], "import")
        db.commit()
    return out


@router.get("/real-accounts/{rid}/report")
async def real_report(rid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    _real(db, rid, user_id)
    db.close()

    def work():
        from app.db.session import get_sessionmaker

        with get_sessionmaker()() as s:
            try:
                return real.report(s, s.get(RealAccountRow, rid))
            except (real.RealError, ValueError) as e:
                return {"status": "错误", "message": str(e)}
    return await asyncio.to_thread(work)
