"""修复已缓存的 A股基金（ETF / LOF）行情：重新下载并按新口径计算复权因子与前收盘价，补录份额折算事件。

数据源口径修正（如 v1.5 加入 ETF 份额折算识别）后运行一次。会写数据库，请先备份 data/stockarena.db：

    cd backend
    uv run python -m app.market.repair
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.arena.deps import market_service
from app.core.config import get_settings
from app.core.network import apply_direct_hosts
from app.db.session import get_sessionmaker
from app.market.service import _instrument_from_row
from app.models.market import InstrumentRow


def repair_cn_funds(progress=print) -> dict[str, str]:
    """返回 {代码: 结果}。"""
    out: dict[str, str] = {}
    with get_sessionmaker()() as s:
        svc = market_service(s, "CN")
        insts = [_instrument_from_row(r) for r in s.scalars(select(InstrumentRow).where(
            InstrumentRow.market == "CN", InstrumentRow.type.in_(("etf", "lof"))).order_by(InstrumentRow.symbol))]
        for inst in insts:
            bars = svc.refetch_bars([inst])[inst.symbol]
            acts = svc.update_corporate_actions([inst])[inst.symbol]
            s.commit()
            out[inst.symbol] = f"日线 {bars}；除权事件 {acts}"
            progress(f"{inst.symbol} {inst.name}：{out[inst.symbol]}")
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_direct_hosts(get_settings().direct_hosts)
    repair_cn_funds()


if __name__ == "__main__":
    main()
