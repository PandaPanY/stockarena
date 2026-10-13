// 按需注册 ECharts 组件，减小打包体积
import { use } from 'echarts/core'
import { BarChart, CandlestickChart, LineChart, RadarChart, ScatterChart } from 'echarts/charts'
import {
  AxisPointerComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([
  LineChart,
  BarChart,
  CandlestickChart,
  ScatterChart,
  RadarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AxisPointerComponent,
  MarkAreaComponent,
  MarkLineComponent,
  CanvasRenderer,
])
