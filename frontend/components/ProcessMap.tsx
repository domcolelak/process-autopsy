import type { ProcessGraph } from "@/lib/api";
import { duration, percent } from "@/lib/format";

/**
 * Process map rendered as plain SVG.
 *
 * Layout is a longest-path layering of the directly-follows graph: each node
 * sits one level below its deepest predecessor, which produces a readable
 * top-to-bottom flow for the acyclic backbone. Edges that point back to an
 * earlier level are drawn as loop edges -- those are exactly the rework paths
 * the product is looking for, so they are highlighted rather than hidden.
 */

const NODE_WIDTH = 210;
const NODE_HEIGHT = 62;
const LEVEL_GAP = 110;
const COLUMN_GAP = 40;
const MARGIN = 28;

interface Placed {
  activity: string;
  x: number;
  y: number;
  level: number;
  node: ProcessGraph["nodes"][number];
}

function layer(graph: ProcessGraph): Map<string, number> {
  const levels = new Map<string, number>();
  const incoming = new Map<string, string[]>();
  for (const node of graph.nodes) {
    levels.set(node.activity, 0);
    incoming.set(node.activity, []);
  }
  for (const edge of graph.edges) {
    if (edge.source === edge.target) continue;
    incoming.get(edge.target)?.push(edge.source);
  }

  // Relaxation over the node count is enough: the longest simple path in a DFG
  // cannot exceed the number of distinct activities.
  for (let pass = 0; pass < graph.nodes.length; pass += 1) {
    let changed = false;
    for (const node of graph.nodes) {
      const parents = incoming.get(node.activity) ?? [];
      let level = levels.get(node.activity) ?? 0;
      for (const parent of parents) {
        const candidate = (levels.get(parent) ?? 0) + 1;
        // Only push forward: a back edge must not drag the whole graph down.
        if (candidate > level && candidate < graph.nodes.length) {
          level = candidate;
          changed = true;
        }
      }
      levels.set(node.activity, level);
    }
    if (!changed) break;
  }
  return levels;
}

function place(graph: ProcessGraph): { nodes: Placed[]; width: number; height: number } {
  const levels = layer(graph);
  const byLevel = new Map<number, ProcessGraph["nodes"]>();
  for (const node of graph.nodes) {
    const level = levels.get(node.activity) ?? 0;
    byLevel.set(level, [...(byLevel.get(level) ?? []), node]);
  }

  const widest = Math.max(...[...byLevel.values()].map((row) => row.length), 1);
  const width = MARGIN * 2 + widest * NODE_WIDTH + (widest - 1) * COLUMN_GAP;
  const depth = Math.max(...byLevel.keys(), 0) + 1;
  const height = MARGIN * 2 + depth * NODE_HEIGHT + (depth - 1) * (LEVEL_GAP - NODE_HEIGHT);

  const nodes: Placed[] = [];
  for (const [level, row] of [...byLevel.entries()].sort((a, b) => a[0] - b[0])) {
    const rowWidth = row.length * NODE_WIDTH + (row.length - 1) * COLUMN_GAP;
    const offset = (width - rowWidth) / 2;
    row.forEach((node, index) => {
      nodes.push({
        activity: node.activity,
        x: offset + index * (NODE_WIDTH + COLUMN_GAP),
        y: MARGIN + level * LEVEL_GAP,
        level,
        node,
      });
    });
  }
  return { nodes, width, height };
}

export default function ProcessMap({ graph }: { graph: ProcessGraph }) {
  if (graph.nodes.length === 0) {
    return <p className="muted">No events have been analysed for this process yet.</p>;
  }

  const { nodes, width, height } = place(graph);
  const position = new Map(nodes.map((node) => [node.activity, node]));
  const maxWait = Math.max(...graph.edges.map((edge) => edge.median_wait_seconds), 1);

  return (
    <div className="map-scroll">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label="Process map"
      >
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--edge)" />
          </marker>
          <marker
            id="arrow-loop"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--warn)" />
          </marker>
        </defs>

        {graph.edges.map((edge) => {
          const from = position.get(edge.source);
          const to = position.get(edge.target);
          if (!from || !to) return null;

          const backwards = to.level <= from.level;
          const x1 = from.x + NODE_WIDTH / 2;
          const y1 = from.y + NODE_HEIGHT;
          const x2 = to.x + NODE_WIDTH / 2;
          const y2 = to.y;
          const bend = backwards ? 90 : 0;
          const path = backwards
            ? `M ${x1} ${y1 - NODE_HEIGHT / 2} C ${x1 + bend} ${y1}, ${x2 + bend} ${y2}, ${x2} ${
                y2 + NODE_HEIGHT / 2
              }`
            : `M ${x1} ${y1} C ${x1} ${y1 + 40}, ${x2} ${y2 - 40}, ${x2} ${y2}`;

          const weight = 1 + (edge.case_count / Math.max(graph.case_count, 1)) * 4;
          const slow = edge.median_wait_seconds > maxWait * 0.6;

          return (
            <g key={`${edge.source}->${edge.target}`} className="edge">
              <path
                d={path}
                fill="none"
                strokeWidth={weight}
                stroke={backwards ? "var(--warn)" : slow ? "var(--danger)" : "var(--edge)"}
                strokeDasharray={backwards ? "6 4" : undefined}
                markerEnd={`url(#${backwards ? "arrow-loop" : "arrow"})`}
              />
              <title>
                {`${edge.source} -> ${edge.target}\n` +
                  `${edge.case_count} cases, median wait ${duration(edge.median_wait_seconds)}, ` +
                  `p90 ${duration(edge.p90_wait_seconds)}, handoff rate ${percent(edge.handoff_rate)}`}
              </title>
            </g>
          );
        })}

        {nodes.map(({ activity, x, y, node }) => (
          <g key={activity} transform={`translate(${x} ${y})`}>
            <rect
              width={NODE_WIDTH}
              height={NODE_HEIGHT}
              rx={10}
              className={node.manual_share > 0.5 ? "node node-manual" : "node"}
            />
            <text x={12} y={24} className="node-title">
              {activity.length > 26 ? `${activity.slice(0, 25)}...` : activity}
            </text>
            <text x={12} y={44} className="node-meta">
              {node.occurrence_count} runs · {duration(node.median_service_seconds)}
              {node.manual_share > 0.5 ? " · manual" : ""}
            </text>
            <title>
              {`${activity}\n${node.occurrence_count} executions across ${node.case_count} cases\n` +
                `median duration ${duration(node.median_service_seconds)}, ` +
                `manual share ${percent(node.manual_share)}`}
            </title>
          </g>
        ))}
      </svg>
    </div>
  );
}
