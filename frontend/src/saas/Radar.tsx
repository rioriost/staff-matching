import type { Scores } from "../saas-models.ts";
import Icon from "../Icon.tsx";
import type { IconName } from "../Icon.tsx";

const axes: { key: keyof Scores; label: string; icon: IconName }[] = [
  { key: "skills", label: "スキル", icon: "star" }, { key: "qualifications", label: "資格", icon: "shield" },
  { key: "preferences", label: "希望", icon: "person" }, { key: "commute", label: "通勤", icon: "route" },
  { key: "history", label: "職歴", icon: "history" },
];
function position(index: number, value: number) {
  const angle = index * Math.PI * 2 / 5 - Math.PI / 2;
  return [130 + Math.cos(angle) * value, 130 + Math.sin(angle) * value];
}
export default function Radar({ scores, name }: { scores: Scores | null; name: string }) {
  if (!scores) return <p>比較評価はありません。適格性の理由をご確認ください。</p>;
  return <div className="saas-radar">
    <svg viewBox="0 0 260 260" aria-hidden="true" focusable="false">
      {[25, 50, 75, 100].map((level) => <polygon key={level} className="saas-radar-grid"
        points={axes.map((_, i) => position(i, level).join(",")).join(" ")} />)}
      {axes.map((axis, i) => {
        const value = scores[axis.key];
        const endpoint = position(i, 100);
        const label = position(i, 119);
        const nextValue = scores[axes[(i + 1) % 5]!.key];
        const dot = value === null ? null : position(i, value);
        const next = nextValue === null ? null : position((i + 1) % 5, nextValue);
        return <g key={axis.key}>
          <line x1="130" y1="130" x2={endpoint[0]} y2={endpoint[1]} className="saas-radar-grid" />
          <text x={label[0]} y={label[1]} textAnchor="middle" dominantBaseline="middle">{axis.label}</text>
          {dot && next && <line x1={dot[0]} y1={dot[1]} x2={next[0]} y2={next[1]} className="saas-radar-value" />}
          {dot && <circle cx={dot[0]} cy={dot[1]} r="4" className="saas-radar-dot" />}
        </g>;
      })}
    </svg>
    <table>
      <caption>{name}の比較評価（100点尺度）</caption>
      <thead><tr><th scope="col">項目</th><th scope="col">値</th></tr></thead>
      <tbody>{axes.map((axis) => <tr key={axis.key}>
        <th scope="row"><Icon name={axis.icon} /> {axis.label}</th>
        <td>{scores[axis.key] === null ? "未確認" : <>{scores[axis.key]?.toFixed(1)}
          <span className="saas-score-track" aria-hidden="true"><span style={{ width: `${scores[axis.key]}%` }} /></span></>}</td>
      </tr>)}</tbody>
    </table>
    {scores.history === null && <p className="saas-hint">職歴は未確認。レーダーの当該点は描かず、0点とは区別します。</p>}
  </div>;
}
