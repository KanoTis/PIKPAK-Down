import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape, {
  type Core,
  type ElementDefinition,
  type EventObject,
} from "cytoscape";
import { Maximize2, Minus, Plus } from "lucide-react";
import type { LineageGraph } from "../../api/client";
import {
  CATEGORY_META,
  CATEGORY_ORDER,
  categoryForNode,
  categoryCounts,
  graphStateLabel,
  type GraphCategory,
} from "./graphTheme";


interface Props {
  graph: LineageGraph;
  selectedId: string | null;
  onSelect: (nodeId: string | null) => void;
}

const NODE_WIDTH = 240;
const NODE_HEIGHT = 118;

function shorten(value: string, max: number): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

function nodeLabel(node: LineageGraph["nodes"][number]): string {
  const category = CATEGORY_META[categoryForNode(node)];
  const title = shorten(node.title || node.share_id, 26);
  const state = node.status || graphStateLabel(node.state);
  return [
    `${category.label} · L${node.depth}`,
    title,
    `${node.file_count} 文件 · ${state}`,
  ].join("\n");
}

const stylesheet = [
  {
    selector: "node",
    style: {
      shape: "round-rectangle",
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      "background-color": "#ffffff",
      "background-opacity": 1,
      "border-width": 2,
      "border-color": "data(color)",
      "border-opacity": 0.9,
      label: "data(label)",
      color: "#0f172a",
      "font-size": 12,
      "font-family": "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif",
      "text-wrap": "wrap",
      "text-max-width": 200,
      "text-valign": "center",
      "text-halign": "center",
      "text-justification": "center",
      "line-height": 1.35,
      "min-zoomed-font-size": 4,
      "overlay-opacity": 0,
    },
  },
  {
    selector: "node.is-start",
    style: {
      "border-width": 3,
    },
  },
  {
    selector: "node.selected",
    style: {
      "border-width": 4,
      "border-opacity": 1,
      "underlay-color": "data(color)",
      "underlay-opacity": 0.12,
      "underlay-padding": 8,
    },
  },
  {
    selector: "node.faded",
    style: {
      opacity: 0.18,
    },
  },
  {
    selector: "node.active",
    style: {
      opacity: 1,
    },
  },
  {
    selector: "edge",
    style: {
      width: 1.4,
      "line-color": "data(color)",
      "target-arrow-color": "data(color)",
      "target-arrow-shape": "triangle",
      "arrow-scale": 0.9,
      "curve-style": "bezier",
      "control-point-step-size": 56,
      opacity: 0.34,
      label: "",
      "font-size": 9,
      color: "#475569",
      "text-background-color": "#ffffff",
      "text-background-opacity": 0.94,
      "text-background-padding": 2,
      "text-rotation": "none",
      "min-zoomed-font-size": 6,
      "overlay-opacity": 0,
    },
  },
  {
    selector: "edge.edge-hidden",
    style: {
      display: "none",
    },
  },
  {
    selector: "edge.edge-default",
    style: {
      display: "element",
      opacity: 0.9,
      width: 2,
      label: "data(label)",
    },
  },
  {
    selector: "edge.edge-active",
    style: {
      display: "element",
      opacity: 1,
      width: 2.6,
      label: "data(label)",
    },
  },
  {
    selector: "edge.edge-faded",
    style: {
      display: "none",
    },
  },
] as cytoscape.StylesheetJson;

export function LineageGraph({ graph, selectedId, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const firstLayoutRef = useRef(false);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const [cyInstance, setCyInstance] = useState<Core | null>(null);
  const [hidden, setHidden] = useState<Set<GraphCategory>>(new Set());
  const counts = useMemo(() => categoryCounts(graph.nodes), [graph.nodes]);

  const elements = useMemo<ElementDefinition[]>(() => {
    const nodes = graph.nodes.filter((node) => !hidden.has(categoryForNode(node)));
    const ids = new Set(nodes.map((node) => node.id));
    const nodeElements: ElementDefinition[] = nodes.map((node) => {
      const category = categoryForNode(node);
      return {
        group: "nodes",
        data: {
          id: node.id,
          label: nodeLabel(node),
          color: CATEGORY_META[category].color,
          category,
          default: node.role === "start",
        },
        classes: `cat-${category}${node.role === "start" ? " is-start" : ""}`,
      };
    });
    const edgeElements: ElementDefinition[] = graph.edges
      .filter((edge) => ids.has(edge.from) && ids.has(edge.to))
      .map((edge) => {
        const target = graph.nodes.find((node) => node.id === edge.to);
        const color = target
          ? CATEGORY_META[categoryForNode(target)].color
          : "#94a3b8";
        const isDefault = edge.from === graph.start;
        return {
          group: "edges",
          data: {
            id: edge.id,
            source: edge.from,
            target: edge.to,
            label: `${edge.file_count} 文件`,
            color,
            default: isDefault,
          },
          classes: isDefault ? "edge-default" : "edge-hidden",
        };
      });
    return [...nodeElements, ...edgeElements];
  }, [graph, hidden]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let cy = cyRef.current;
    if (!cy) {
      cy = cytoscape({
        container,
        elements: [],
        style: stylesheet,
        layout: { name: "preset" },
        wheelSensitivity: 0.18,
        minZoom: 0.05,
        maxZoom: 2.5,
        boxSelectionEnabled: false,
        autoungrabify: true,
        userPanningEnabled: true,
        userZoomingEnabled: true,
      });
      cyRef.current = cy;
      setCyInstance(cy);

      const restore = () => {
        cy!.elements().removeClass("faded active");
        cy!.edges()
          .removeClass("edge-active edge-faded")
          .addClass("edge-hidden")
          .removeClass("edge-default");
        cy!.edges()
          .filter((edge) => edge.data("default") === true)
          .removeClass("edge-hidden")
          .addClass("edge-default");
      };

      cy.on("mouseover", "node", (event: EventObject) => {
        const node = event.target;
        const edges = node.connectedEdges();
        cy!.elements().addClass("faded");
        cy!.elements().removeClass("active");
        node.removeClass("faded").addClass("active");
        if (edges.length > 8) {
          cy!.edges()
            .removeClass("edge-active edge-default")
            .addClass("edge-hidden");
          return;
        }
        edges.connectedNodes().union(node).removeClass("faded").addClass("active");
        edges
          .removeClass("faded edge-hidden edge-default")
          .addClass("edge-active");
        cy!.edges()
          .not(edges)
          .removeClass("edge-active edge-default")
          .addClass("edge-faded edge-hidden");
      });
      cy.on("mouseout", "node", restore);
      cy.on("mouseover", "edge", (event: EventObject) => {
        const edge = event.target;
        const endpoints = edge.source().union(edge.target());
        cy!.elements().addClass("faded");
        endpoints.removeClass("faded").addClass("active");
        edge
          .removeClass("faded edge-hidden edge-default")
          .addClass("edge-active");
        cy!.edges()
          .not(edge)
          .removeClass("edge-active edge-default")
          .addClass("edge-faded edge-hidden");
      });
      cy.on("mouseout", "edge", restore);
      cy.on("tap", "node", (event: EventObject) => {
        onSelectRef.current(event.target.id());
      });
      cy.on("tap", "edge", (event: EventObject) => {
        onSelectRef.current(event.target.target().id());
      });
      cy.on("tap", (event: EventObject) => {
        if (event.target === cy) onSelectRef.current(null);
      });

      const observer = new ResizeObserver(() => cy!.resize());
      observer.observe(container);
    }

    cy.elements().remove();
    cy.add(elements);

    const visibleNodes = graph.nodes
      .filter((node) => !hidden.has(categoryForNode(node)))
      .slice()
      .sort((a, b) => {
        if (a.depth !== b.depth) return a.depth - b.depth;
        return (a.title || a.share_id).localeCompare(b.title || b.share_id);
      });
    const depthRows = new Map<number, number>();
    for (const node of visibleNodes) {
      const row = depthRows.get(node.depth) ?? 0;
      depthRows.set(node.depth, row + 1);
      const element = cy.getElementById(node.id);
      if (element.nonempty()) {
        element.position({
          x: node.depth * (NODE_WIDTH + 100),
          y: row * (NODE_HEIGHT + 14),
        });
      }
    }
    cy.layout({ name: "preset", fit: false, animate: false }).run();

    window.requestAnimationFrame(() => {
      if (!cy) return;
      if (!firstLayoutRef.current && cy.nodes().length > 0) {
        firstLayoutRef.current = true;
        if (graph.nodes.length <= 8) {
          cy.fit(undefined, 50);
          const pan = cy.pan();
          cy.pan({ x: pan.x, y: pan.y + 120 });
        } else {
          const bb = cy.elements().boundingBox();
          const width = container.clientWidth || 1128;
          const zoom = Math.min(0.9, Math.max(0.2, (width - 100) / Math.max(bb.w, 1)));
          cy.viewport({
            zoom,
            pan: { x: 40 - bb.x1 * zoom, y: 240 - bb.y1 * zoom },
          });
        }
      }
    });
  }, [elements, graph.nodes.length]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().removeClass("selected");
    if (selectedId) cy.$id(selectedId).addClass("selected");
  }, [selectedId, elements]);

  const toggleCategory = (category: GraphCategory) => {
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      return next;
    });
  };

  const fit = () => {
    const cy = cyRef.current;
    if (!cy) return;
    const container = containerRef.current;
    if (!container) return;
    const bb = cy.elements().boundingBox();
    const width = container.clientWidth || 1128;
    const zoom = Math.min(0.9, Math.max(0.2, (width - 100) / Math.max(bb.w, 1)));
    cy.viewport({ zoom, pan: { x: 40 - bb.x1 * zoom, y: 240 - bb.y1 * zoom } });
  };

  return (
    <div className="graph-canvas">
      <div ref={containerRef} className="graph-cy" />
      <div className="graph-legend">
        <div className="graph-legend__title">节点状态</div>
        <div className="graph-legend__items">
          {CATEGORY_ORDER.map((category) => {
            const meta = CATEGORY_META[category];
            const Icon = meta.icon;
            const active = !hidden.has(category);
            return (
              <button
                key={category}
                type="button"
                className={`graph-legend__item${active ? "" : " is-off"}`}
                onClick={() => toggleCategory(category)}
                aria-pressed={active}
              >
                <span className="graph-legend__dot" style={{ background: meta.color }} />
                <Icon size={12} aria-hidden />
                {meta.label}
                <span className="graph-legend__count">{counts[category]}</span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="graph-stats">
        <span className="graph-stats__hint">悬停节点高亮 · 连接过多时用详情看来源</span>
        <span>{graph.stats.shares} 个分享</span>
        <span>{graph.stats.edges} 条派生</span>
        <span>{graph.stats.files} 个文件</span>
        <span>{graph.stats.folders} 个目录</span>
      </div>
      <div className="graph-controls">
        <button
          type="button"
          onClick={() => cyInstance?.zoom(cyInstance.zoom() * 1.2)}
          aria-label="放大"
        >
          <Plus size={14} aria-hidden />
        </button>
        <button
          type="button"
          onClick={() => cyInstance?.zoom(cyInstance.zoom() / 1.2)}
          aria-label="缩小"
        >
          <Minus size={14} aria-hidden />
        </button>
        <button type="button" onClick={fit} aria-label="适应画布">
          <Maximize2 size={14} aria-hidden />
        </button>
      </div>
      {elements.length === 0 ? (
        <div className="graph-empty">
          <div className="graph-empty__card">没有可展示的节点</div>
        </div>
      ) : null}
    </div>
  );
}
