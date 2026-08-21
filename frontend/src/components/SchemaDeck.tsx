import { useEffect, useRef, useState } from "react";
import type { SchemaColumn, SchemaTable } from "../types";

type SchemaDeckProps = {
  tables: SchemaTable[];
};

const HOVER_DELAY_MS = 90;

export default function SchemaDeck({ tables }: SchemaDeckProps) {
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [finePointer, setFinePointer] = useState(false);
  const hoverTimer = useRef<number | null>(null);

  useEffect(() => {
    const media = window.matchMedia("(hover: hover) and (pointer: fine)");
    const sync = () => setFinePointer(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setPinned(null);
      setHovered(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const current = pinned ?? (finePointer ? hovered : null);
  const activeIndex = tables.findIndex((table) => table.name === current);
  const split = current !== null;

  function scheduleHover(name: string) {
    if (!finePointer) return;
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => setHovered(name), HOVER_DELAY_MS);
  }

  function clearHover() {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    setHovered(null);
  }

  return (
    <div className="db-cylinder mt-4" onMouseLeave={clearHover}>
      <div className="db-lid" aria-hidden="true" />
      {tables.map((table, index) => {
        const expanded = current === table.name;
        const shift =
          !split || activeIndex < 0
            ? 0
            : index < activeIndex
              ? -1
              : index > activeIndex
                ? 1
                : 0;
        const keys = keyColumns(table.columns);
        const extras = extraColumns(table.columns);

        return (
          <button
            key={table.name}
            type="button"
            className={`db-slice ${expanded ? "is-open" : ""}`}
            style={{
              zIndex: tables.length - index,
              transform: `translateY(${shift * 22}px)`,
            }}
            aria-expanded={expanded}
            onMouseEnter={() => scheduleHover(table.name)}
            onFocus={() => setHovered(table.name)}
            onClick={() =>
              setPinned((value) => (value === table.name ? null : table.name))
            }
          >
            <span className="db-slice-name">{table.name}</span>
            <span className={`db-slice-keys ${expanded ? "is-visible" : ""}`}>
              <span className="db-slice-keys-inner">
                <span className="db-key-row">
                  {keys.map((column) => (
                    <span
                      key={column.name}
                      className={column.is_primary_key ? "db-key is-pk" : "db-key is-fk"}
                      title={keyTitle(column)}
                    >
                      <span className="db-key-kind">
                        {column.is_primary_key ? "PK" : "FK"}
                      </span>
                      <span className="db-key-name">{column.name}</span>
                      {column.foreign_key && (
                        <span className="db-key-ref">→ {column.foreign_key.table}</span>
                      )}
                    </span>
                  ))}
                </span>
                {extras.length > 0 && (
                  <span className="db-key-rest">{extras.map((column) => column.name).join(" · ")}</span>
                )}
              </span>
            </span>
          </button>
        );
      })}
      <p className="db-legend">
        <span>
          <b>PK</b> clave primaria
        </span>
        <span>
          <b>FK</b> clave foránea
        </span>
        <span className="db-legend-hint">
          {finePointer ? "Hover para ver · clic para fijar" : "Tocá una capa para ver sus keys"}
        </span>
      </p>
    </div>
  );
}

function keyColumns(columns: SchemaColumn[]): SchemaColumn[] {
  const keys = columns.filter((column) => column.is_primary_key || column.foreign_key);
  return keys.length > 0 ? keys : columns.slice(0, 1);
}

function extraColumns(columns: SchemaColumn[]): SchemaColumn[] {
  return columns.filter((column) => !column.is_primary_key && !column.foreign_key);
}

function keyTitle(column: SchemaColumn): string {
  if (column.is_primary_key) return `Clave primaria: ${column.name}`;
  if (column.foreign_key) {
    return `Clave foránea: ${column.name} → ${column.foreign_key.table}.${column.foreign_key.column}`;
  }
  return column.name;
}
