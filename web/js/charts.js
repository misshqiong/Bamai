import {formatNumber, t} from "./i18n.js";

const palette = {cyan: "#38d7ff", green: "#42e59e", violet: "#9b8cff", amber: "#ffc857"};
const axis = {axisLine: {lineStyle: {color: "#263241"}}, axisLabel: {color: "#748397", fontSize: 9}, splitLine: {lineStyle: {color: "#1d2632"}}};

function baseOption(series, valueFormatter = value => value, yFormatter = null) {
  return {
    animation: false,
    grid: {left: 56, right: 12, top: 14, bottom: 28},
    tooltip: {trigger: "axis", backgroundColor: "#121923", borderColor: "#2a3747", textStyle: {color: "#e8eef5", fontSize: 11}, valueFormatter},
    xAxis: {type: "time", ...axis, splitLine: {show: false}},
    yAxis: {type: "value", ...axis, axisLabel: {...axis.axisLabel, formatter: yFormatter || (value => value)}},
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
    this.cpuMemory.setOption(baseOption([line(t("chart.cpu"), palette.cyan), line(t("chart.memory"), palette.violet)], value => `${formatNumber(value, {maximumFractionDigits: 1})}%`, value => `${formatNumber(value)}%`));
    this.network.setOption(baseOption([line(t("chart.upload"), palette.green), line(t("chart.download"), palette.cyan)], formatRate, formatBytes));
    this.disk.setOption(baseOption([line(t("chart.read"), palette.amber), line(t("chart.write"), palette.violet)], formatRate, formatBytes));
    this.core.setOption(baseOption([], value => `${formatNumber(value, {maximumFractionDigits: 1})}%`, value => `${formatNumber(value)}%`));
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
    // notMerge 全量替换时必须带上完整 option，否则坐标轴配置会被清空导致 ECharts 抛错
    const coreSeries = Array.from({length: count}, (_, index) => ({...line(t("chart.core", {number: formatNumber(index + 1)}), [palette.cyan, palette.green, palette.violet, palette.amber][index % 4]), data: this.points.map(item => [item.ts * 1000, item.cpu_per_core?.[index] || 0])}));
    this.core.setOption(baseOption(coreSeries, value => `${formatNumber(value, {maximumFractionDigits: 1})}%`, value => `${formatNumber(value)}%`), true);
  }
  resize() { this.cpuMemory.resize(); this.network.resize(); this.disk.resize(); this.core.resize(); }
  translate() {
    this.cpuMemory.setOption({series: [{name: t("chart.cpu")}, {name: t("chart.memory")}]});
    this.network.setOption({series: [{name: t("chart.upload")}, {name: t("chart.download")}]});
    this.disk.setOption({series: [{name: t("chart.read")}, {name: t("chart.write")}]});
    if (this.points.length) this.push({...this.points.pop()});
  }
}

export class HistoryChart {
  constructor() {
    this.chart = echarts.init(document.querySelector("#history-chart"));
    addEventListener("resize", () => this.chart.resize());
  }
  set(metric, points) {
    const rates = metric.includes("bps");
    this.chart.setOption(baseOption([
      {...line(t("chart.average"), palette.cyan), data: points.map(point => [point.ts * 1000, point.avg])},
      {...line(t("chart.peak"), palette.amber), areaStyle: undefined, lineStyle: {width: 1, type: "dashed", color: palette.amber}, data: points.map(point => [point.ts * 1000, point.max])},
    ], rates ? formatRate : value => formatNumber(value, {maximumFractionDigits: 1}), rates ? formatBytes : null), true);
  }
}

export class AppHistoryChart {
  constructor() {
    this.chart = echarts.init(document.querySelector("#app-history-chart"));
    this.points = [];
    addEventListener("resize", () => this.resize());
  }
  set(points) {
    this.points = points;
    const option = baseOption([
      {...line(t("chart.cpu"), palette.cyan), yAxisIndex: 0, data: points.map(point => [point.ts * 1000, point.cpu_percent])},
      {...line(t("chart.memory"), palette.violet), yAxisIndex: 1, data: points.map(point => [point.ts * 1000, point.memory_rss])},
    ], value => formatNumber(value, {maximumFractionDigits: 1}));
    option.grid.right = 68;
    option.yAxis = [
      {type: "value", ...axis, axisLabel: {...axis.axisLabel, formatter: value => `${formatNumber(value)}%`}},
      {type: "value", ...axis, axisLabel: {...axis.axisLabel, formatter: formatBytes}},
    ];
    this.chart.setOption(option, true);
  }
  resize() { this.chart.resize(); }
  translate() { this.set(this.points); }
}

export function formatBytes(value) {
  if (value == null) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let number = Number(value), index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index++; }
  return `${formatNumber(number, {maximumFractionDigits: number >= 100 ? 0 : 1})} ${units[index]}`;
}
export function formatRate(value) { return `${formatBytes(value)}/s`; }
