const palette = {cyan: "#38d7ff", green: "#42e59e", violet: "#9b8cff", amber: "#ffc857"};
const axis = {axisLine: {lineStyle: {color: "#263241"}}, axisLabel: {color: "#748397", fontSize: 9}, splitLine: {lineStyle: {color: "#1d2632"}}};

function baseOption(series, valueFormatter = value => value) {
  return {
    animation: false,
    grid: {left: 43, right: 12, top: 14, bottom: 28},
    tooltip: {trigger: "axis", backgroundColor: "#121923", borderColor: "#2a3747", textStyle: {color: "#e8eef5", fontSize: 11}, valueFormatter},
    xAxis: {type: "time", ...axis, splitLine: {show: false}},
    yAxis: {type: "value", ...axis},
    series,
  };
}

function line(name, color) {
  return {name, type: "line", showSymbol: false, smooth: .2, lineStyle: {width: 1.7, color}, itemStyle: {color}, areaStyle: {color: `${color}18`}, data: []};
}

export class RealtimeCharts {
  constructor() {
    this.cpuMemory = echarts.init(document.querySelector("#cpu-memory-chart"));
    this.network = echarts.init(document.querySelector("#network-chart"));
    this.disk = echarts.init(document.querySelector("#disk-io-chart"));
    this.core = echarts.init(document.querySelector("#core-chart"));
    this.points = [];
    this.cpuMemory.setOption(baseOption([line("CPU %", palette.cyan), line("内存 %", palette.violet)], value => `${Number(value).toFixed(1)}%`));
    this.network.setOption(baseOption([line("上传", palette.green), line("下载", palette.cyan)], formatRate));
    this.disk.setOption(baseOption([line("读取", palette.amber), line("写入", palette.violet)], formatRate));
    this.core.setOption(baseOption([]));
    addEventListener("resize", () => this.resize());
  }
  push(metric) {
    this.points.push(metric);
    const cutoff = Math.floor(Date.now() / 1000) - 300;
    this.points = this.points.filter(item => item.ts >= cutoff);
    const values = key => this.points.map(item => [item.ts * 1000, item[key]]);
    this.cpuMemory.setOption({series: [{data: values("cpu_percent")}, {data: values("mem_percent")}]});
    this.network.setOption({series: [{data: values("net_up_bps")}, {data: values("net_down_bps")}]});
    this.disk.setOption({series: [{data: values("disk_read_bps")}, {data: values("disk_write_bps")}]});
    const count = metric.cpu_per_core?.length || 0;
    this.core.setOption({series: Array.from({length: count}, (_, index) => ({...line(`核心 ${index + 1}`, [palette.cyan, palette.green, palette.violet, palette.amber][index % 4]), data: this.points.map(item => [item.ts * 1000, item.cpu_per_core?.[index] || 0])}))}, true);
  }
  resize() { this.cpuMemory.resize(); this.network.resize(); this.disk.resize(); this.core.resize(); }
}

export class HistoryChart {
  constructor() {
    this.chart = echarts.init(document.querySelector("#history-chart"));
    addEventListener("resize", () => this.chart.resize());
  }
  set(metric, points) {
    const rates = metric.includes("bps");
    this.chart.setOption(baseOption([
      {...line("平均", palette.cyan), data: points.map(point => [point.ts * 1000, point.avg])},
      {...line("峰值", palette.amber), areaStyle: undefined, lineStyle: {width: 1, type: "dashed", color: palette.amber}, data: points.map(point => [point.ts * 1000, point.max])},
    ], rates ? formatRate : value => Number(value).toFixed(1)), true);
  }
}

export function formatBytes(value) {
  if (value == null) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let number = Number(value), index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index++; }
  return `${number >= 100 ? number.toFixed(0) : number.toFixed(1)} ${units[index]}`;
}
export function formatRate(value) { return `${formatBytes(value)}/s`; }

