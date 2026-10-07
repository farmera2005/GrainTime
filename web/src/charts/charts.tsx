// Small SVG chart kit for the dashboard. Marks follow the data-viz specs:
// thin marks (bars <= 24px, 4px rounded data end, square at the baseline),
// 2px lines, 8px markers with a 2px surface ring, hairline solid grid,
// one y-axis only, text in ink tokens (never series colour), a hover layer
// on every chart, and a legend whenever there are two series.

import { ReactNode, useEffect, useRef, useState } from "react";

export function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => setW(Math.max(200, Math.floor(e[0].contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

// Axis maximum and ticks on round steps (1, 2, 5 x 10^k), at most ~5 gridlines.
function niceStep(v: number): number {
  const raw = Math.max(v, 1) / 4;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return Math.max(1, m * p);
  return 10 * p;
}

function niceMax(v: number): number {
  const st = niceStep(v);
  return Math.max(st, Math.ceil(v / st) * st);
}

function ticks(max: number): number[] {
  const st = niceStep(max);
  const out: number[] = [];
  for (let t = 0; t <= max + 1e-9; t += st) out.push(t);
  return out;
}

type Tip = { x: number; y: number; content: ReactNode } | null;

function Tooltip({ tip }: { tip: Tip }) {
  if (!tip) return null;
  return (
    <div className="viz-tip" style={{ left: tip.x, top: tip.y }} role="status">
      {tip.content}
    </div>
  );
}

export function TipRow(props: { color?: string; value: ReactNode; label: string; line?: boolean }) {
  return (
    <div className="viz-tip-row">
      {props.color ? <span className={props.line ? "viz-key-line" : "viz-key-rect"} style={{ background: props.color }} /> : null}
      <strong>{props.value}</strong> <span className="viz-tip-label">{props.label}</span>
    </div>
  );
}

const M = { top: 12, right: 16, bottom: 26, left: 40 };

// --------------------------------------------------------------------------- //
// Columns (vertical bars) - one series, optional reference lines
// --------------------------------------------------------------------------- //

export function Columns(props: {
  data: { label: string; value: number | null; tip?: ReactNode }[];
  height?: number;
  unit?: string;
  color?: string;
  refLines?: { value: number; label: string }[];
  labelEvery?: number;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip>(null);
  const [hover, setHover] = useState<number | null>(null);
  const h = props.height ?? 180;
  const iw = width - M.left - M.right;
  const ih = h - M.top - M.bottom;
  const vmax = niceMax(Math.max(1, ...props.data.map((d) => d.value ?? 0), ...(props.refLines ?? []).map((r) => r.value)));
  const band = iw / Math.max(1, props.data.length);
  const bw = Math.min(24, Math.max(2, band - 2));
  const y = (v: number) => M.top + ih - (v / vmax) * ih;
  const every = props.labelEvery ?? Math.max(1, Math.ceil(props.data.length / Math.max(1, Math.floor(iw / 40))));
  return (
    <div ref={ref} className="viz" onPointerLeave={() => { setTip(null); setHover(null); }}>
      <svg width={width} height={h} role="img" aria-label={props.ariaLabel}>
        {ticks(vmax).map((t) => (
          <g key={t}>
            <line x1={M.left} x2={width - M.right} y1={y(t)} y2={y(t)} className="viz-grid" />
            <text x={M.left - 6} y={y(t) + 4} className="viz-axis" textAnchor="end">{t}</text>
          </g>
        ))}
        {props.data.map((d, i) => {
          const x = M.left + i * band + (band - bw) / 2;
          const v = d.value ?? 0;
          const top = y(v);
          const r = Math.min(4, bw / 2, (M.top + ih - top) / 2);
          const path = d.value ? `M${x},${M.top + ih} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${M.top + ih} Z` : "";
          return (
            <g key={i}>
              {path ? <path d={path} fill={props.color ?? "var(--series-1)"} opacity={hover === null || hover === i ? 1 : 0.55} /> : null}
              {i % every === 0 ? <text x={M.left + i * band + band / 2} y={h - 8} className="viz-axis" textAnchor="middle">{d.label}</text> : null}
              <rect
                x={M.left + i * band} y={M.top} width={band} height={ih} fill="transparent"
                tabIndex={0}
                onPointerMove={(e) => { setHover(i); setTip({ x: e.nativeEvent.offsetX + 12, y: e.nativeEvent.offsetY - 10, content: d.tip ?? <TipRow value={d.value ?? "-"} label={d.label} /> }); }}
                onFocus={() => { setHover(i); setTip({ x: M.left + i * band, y: M.top, content: d.tip ?? <TipRow value={d.value ?? "-"} label={d.label} /> }); }}
                onBlur={() => { setHover(null); setTip(null); }}
              />
            </g>
          );
        })}
        {(props.refLines ?? []).map((r) => (
          <g key={r.label}>
            <line x1={M.left} x2={width - M.right} y1={y(r.value)} y2={y(r.value)} className="viz-ref" />
            <text x={width - M.right} y={y(r.value) - 4} className="viz-axis" textAnchor="end">{r.label}</text>
          </g>
        ))}
        <line x1={M.left} x2={width - M.right} y1={M.top + ih} y2={M.top + ih} className="viz-baseline" />
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Lines over time - up to 2 series, crosshair tooltip, legend + end labels
// --------------------------------------------------------------------------- //

export type Series = { name: string; color: string; values: (number | null)[] };

export function Lines(props: { labels: string[]; series: Series[]; height?: number; unit: string; ariaLabel: string; refLines?: { value: number; label: string }[] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [idx, setIdx] = useState<number | null>(null);
  const h = props.height ?? 220;
  const right = 64;
  const iw = width - M.left - right;
  const ih = h - M.top - M.bottom;
  const all = props.series.flatMap((s) => s.values.filter((v): v is number => v != null));
  const vmax = niceMax(Math.max(1, ...all, ...(props.refLines ?? []).map((r) => r.value)));
  const n = props.labels.length;
  const x = (i: number) => M.left + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v: number) => M.top + ih - (v / vmax) * ih;
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(iw / 70))));
  const pathFor = (vals: (number | null)[]) => {
    let d = "";
    let pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const lastIdx = (vals: (number | null)[]) => { for (let i = vals.length - 1; i >= 0; i--) if (vals[i] != null) return i; return -1; };
  return (
    <div className="viz-wrap">
      {props.series.length > 1 ? (
        <div className="viz-legend">
          {props.series.map((s) => (
            <span key={s.name}><span className="viz-key-line" style={{ background: s.color }} />{s.name}</span>
          ))}
        </div>
      ) : null}
      <div ref={ref} className="viz"
        onPointerMove={(e) => {
          const px = e.nativeEvent.offsetX;
          const i = Math.round(((px - M.left) / Math.max(1, iw)) * (n - 1));
          setIdx(Math.max(0, Math.min(n - 1, i)));
        }}
        onPointerLeave={() => setIdx(null)}
      >
        <svg width={width} height={h} role="img" aria-label={props.ariaLabel}>
          {ticks(vmax).map((t) => (
            <g key={t}>
              <line x1={M.left} x2={M.left + iw} y1={y(t)} y2={y(t)} className="viz-grid" />
              <text x={M.left - 6} y={y(t) + 4} className="viz-axis" textAnchor="end">{t}</text>
            </g>
          ))}
          {(props.refLines ?? []).map((r) => (
            <g key={r.label}>
              <line x1={M.left} x2={M.left + iw} y1={y(r.value)} y2={y(r.value)} className="viz-ref" />
              <text x={M.left + iw} y={y(r.value) - 4} className="viz-axis" textAnchor="end">{r.label}</text>
            </g>
          ))}
          {props.labels.map((l, i) => (i % every === 0 ? <text key={i} x={x(i)} y={h - 8} className="viz-axis" textAnchor="middle">{l}</text> : null))}
          <line x1={M.left} x2={M.left + iw} y1={M.top + ih} y2={M.top + ih} className="viz-baseline" />
          {props.series.map((s) => (
            <path key={s.name} d={pathFor(s.values)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {props.series.map((s) => {
            const li = lastIdx(s.values);
            if (li < 0) return null;
            return (
              <g key={s.name + "-end"}>
                <circle cx={x(li)} cy={y(s.values[li]!)} r={4} fill={s.color} stroke="var(--viz-surface)" strokeWidth={2} />
                <text x={x(li) + 8} y={y(s.values[li]!) + 4} className="viz-label">{s.values[li]} {props.unit}</text>
              </g>
            );
          })}
          {idx != null ? (
            <g>
              <line x1={x(idx)} x2={x(idx)} y1={M.top} y2={M.top + ih} className="viz-crosshair" />
              {props.series.map((s) => (s.values[idx] != null ? <circle key={s.name} cx={x(idx)} cy={y(s.values[idx]!)} r={4} fill={s.color} stroke="var(--viz-surface)" strokeWidth={2} /> : null))}
            </g>
          ) : null}
        </svg>
        {idx != null ? (
          <div className="viz-tip" style={{ left: Math.min(x(idx) + 12, width - 170), top: M.top }}>
            <div className="viz-tip-title">{props.labels[idx]}</div>
            {props.series.map((s) => (
              <TipRow key={s.name} color={s.color} line value={s.values[idx] != null ? `${s.values[idx]} ${props.unit}` : "no trucks"} label={s.name} />
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Horizontal bars - one per site, value at the tip
// --------------------------------------------------------------------------- //

export function HBars(props: { data: { label: string; value: number | null; tip?: ReactNode }[]; unit: string; ariaLabel: string; refLines?: { value: number; label: string }[] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip>(null);
  const labelW = Math.min(160, Math.max(60, ...props.data.map((d) => d.label.length * 7.5)));
  const row = 30;
  const h = props.data.length * row + 28;
  const iw = width - labelW - 70;
  const vmax = niceMax(Math.max(1, ...props.data.map((d) => d.value ?? 0), ...(props.refLines ?? []).map((r) => r.value)));
  const x = (v: number) => labelW + (v / vmax) * iw;
  return (
    <div ref={ref} className="viz" onPointerLeave={() => setTip(null)}>
      <svg width={width} height={h} role="img" aria-label={props.ariaLabel}>
        {ticks(vmax).map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={4} y2={h - 22} className="viz-grid" />
            <text x={x(t)} y={h - 6} className="viz-axis" textAnchor="middle">{t}</text>
          </g>
        ))}
        {(props.refLines ?? []).map((r) => (
          <g key={r.label}>
            <line x1={x(r.value)} x2={x(r.value)} y1={4} y2={h - 22} className="viz-ref" />
          </g>
        ))}
        {props.data.map((d, i) => {
          const yy = 6 + i * row;
          const bh = Math.min(18, row - 8);
          const end = d.value != null ? x(d.value) : labelW;
          const r = Math.min(4, bh / 2, (end - labelW) / 2);
          const path = d.value ? `M${labelW},${yy} H${end - r} Q${end},${yy} ${end},${yy + r} V${yy + bh - r} Q${end},${yy + bh} ${end - r},${yy + bh} H${labelW} Z` : "";
          return (
            <g key={d.label}>
              <text x={labelW - 8} y={yy + bh / 2 + 4} className="viz-label" textAnchor="end">{d.label}</text>
              {path ? <path d={path} fill="var(--series-1)" /> : null}
              <text x={end + 6} y={yy + bh / 2 + 4} className="viz-label">{d.value != null ? `${d.value} ${props.unit}` : "no trucks"}</text>
              <rect x={0} y={yy - 4} width={width} height={row} fill="transparent" tabIndex={0}
                onPointerMove={(e) => setTip({ x: e.nativeEvent.offsetX + 12, y: e.nativeEvent.offsetY - 10, content: d.tip ?? <TipRow value={d.value ?? "-"} label={d.label} /> })}
                onFocus={() => setTip({ x: labelW, y: yy, content: d.tip ?? <TipRow value={d.value ?? "-"} label={d.label} /> })}
                onBlur={() => setTip(null)} />
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Heatmap - hour of day x day of week, sequential single hue
// --------------------------------------------------------------------------- //

const SEQ = ["var(--seq-150)", "var(--seq-250)", "var(--seq-350)", "var(--seq-450)", "var(--seq-550)", "var(--seq-650)"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function hourLabel(h: number): string {
  return h === 0 ? "12a" : h < 12 ? `${h}a` : h === 12 ? "12p" : `${h - 12}p`;
}

export function Heatmap(props: { cells: { dow: number; hour: number; value: number | null; count: number }[]; unit: string; ariaLabel: string }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip>(null);
  const withData = props.cells.filter((c) => c.value != null);
  const hours = withData.length ? withData.map((c) => c.hour) : [6, 18];
  const h0 = Math.max(0, Math.min(...hours) - 1), h1 = Math.min(23, Math.max(...hours) + 1);
  const cols = h1 - h0 + 1;
  const left = 38, top = 4;
  const cw = Math.max(10, (width - left - 4) / cols);
  const ch = 22;
  const vals = withData.map((c) => c.value as number);
  const vmin = vals.length ? Math.min(...vals) : 0, vmax = vals.length ? Math.max(...vals) : 1;
  const step = (v: number) => SEQ[Math.min(SEQ.length - 1, Math.floor(((v - vmin) / Math.max(0.001, vmax - vmin)) * SEQ.length))];
  const lookup = new Map(props.cells.map((c) => [`${c.dow}-${c.hour}`, c]));
  const height = top + 7 * (ch + 2) + 22;
  return (
    <div className="viz-wrap">
      <div ref={ref} className="viz" onPointerLeave={() => setTip(null)}>
        <svg width={width} height={height} role="img" aria-label={props.ariaLabel}>
          {DAYS.map((d, r) => <text key={d} x={left - 6} y={top + r * (ch + 2) + ch / 2 + 4} className="viz-axis" textAnchor="end">{d}</text>)}
          {Array.from({ length: cols }, (_, i) => h0 + i).map((hh, i) => (
            <g key={hh}>
              {i % Math.max(1, Math.ceil(28 / cw)) === 0 ? <text x={left + i * cw + cw / 2} y={height - 6} className="viz-axis" textAnchor="middle">{hourLabel(hh)}</text> : null}
              {DAYS.map((_, r) => {
                const c = lookup.get(`${r}-${hh}`);
                const fill = c && c.value != null ? step(c.value) : "var(--viz-empty)";
                return (
                  <rect key={r} x={left + i * cw + 1} y={top + r * (ch + 2)} width={cw - 2} height={ch} rx={3} fill={fill} tabIndex={0}
                    onPointerMove={(e) => setTip({ x: e.nativeEvent.offsetX + 12, y: e.nativeEvent.offsetY - 10, content: (
                      <><div className="viz-tip-title">{DAYS[r]} {hourLabel(hh)}</div><TipRow value={c?.value != null ? `${c.value} ${props.unit}` : "no trucks"} label={c ? `median, ${c.count} trucks` : ""} /></>) })}
                    onFocus={() => setTip({ x: left + i * cw, y: top + r * (ch + 2), content: <TipRow value={c?.value != null ? `${c.value} ${props.unit}` : "no trucks"} label={`${DAYS[r]} ${hourLabel(hh)}`} /> })}
                    onBlur={() => setTip(null)} />
                );
              })}
            </g>
          ))}
        </svg>
        <Tooltip tip={tip} />
      </div>
      {vals.length ? (
        <div className="viz-scale">
          <span>{vmin} {props.unit}</span>
          {SEQ.map((c) => <span key={c} className="viz-scale-step" style={{ background: c }} />)}
          <span>{vmax} {props.unit}</span>
        </div>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Table view (every chart has one)
// --------------------------------------------------------------------------- //

export function DataTable(props: { columns: { key: string; label: string }[]; rows: Record<string, unknown>[] }) {
  return (
    <div className="table-wrap">
      <table className="num-table">
        <thead><tr>{props.columns.map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead>
        <tbody>
          {props.rows.map((r, i) => (
            <tr key={i}>{props.columns.map((c) => <td key={c.key}>{r[c.key] == null ? "" : String(r[c.key])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
