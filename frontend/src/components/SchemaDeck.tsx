import { useState } from "react";
import type { SchemaColumn, SchemaTable } from "../types";

type SchemaDeckProps = {
  tables: SchemaTable[];
};

export default function SchemaDeck({ tables }: SchemaDeckProps) {
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const current = pinned ?? hovered;
  const activeIndex = tables.findIndex((table) => table.name === current);
  const split = current !== null;

  return (
    <div
      className="db-cylinder mt-5"
      onMouseLeave={() => setHovered(null)}
    >
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
            className={`db-slice ${index === 0 ? "is-top" : ""} ${expanded ? "is-open" : ""}`}
            style={{
              zIndex: tables.length - index,
              transform: `translateY(${shift * 26}px)`,
            }}
            aria-expanded={expanded}
            aria-label={`Entidad ${table.name}`}
            onMouseEnter={() => setHovered(table.name)}
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
                    >
                      <KeySvg />
                      <span>{column.name}</span>
                      {column.foreign_key && (
                        <span className="db-key-ref">→ {column.foreign_key.table}</span>
                      )}
                    </span>
                  ))}
                </span>
                {extras.length > 0 && (
                  <span className="db-key-rest">
                    {extras.map((column) => column.name).join(" · ")}
                  </span>
                )}
              </span>
            </span>
          </button>
        );
      })}
      <p className="mt-6 text-center text-[11px] text-slate-400">
        Pasá el cursor sobre una capa para ver sus keys.
      </p>
    </div>
  );
}

function keyColumns(columns: SchemaColumn[]): SchemaColumn[] {
  const keys = columns.filter((column) => column.is_primary_key || column.foreign_key);
  return keys.length > 0 ? keys : columns.slice(0, 1);
}

function extraColumns(columns: SchemaColumn[]): SchemaColumn[] {
  const rest = columns.filter((column) => !column.is_primary_key && !column.foreign_key);
  return rest.slice(0, 6);
}

function KeySvg() {
  return (
    <svg viewBox="0 0 16 16" className="h-3 w-3 shrink-0" fill="currentColor" aria-hidden="true">
      <path d="M10.2 1.5a4.3 4.3 0 0 0-3.7 6.5L1.4 13.1a.8.8 0 0 0-.2.5v1.6c0 .4.4.8.8.8h1.7c.2 0 .4-.1.5-.2l.6-.6h.9v-.9h.9v-.9h.9l1.5-1.5a4.3 4.3 0 0 0 1.2-8.4Zm0 2.2a2.1 2.1 0 1 1 0 4.2 2.1 2.1 0 0 1 0-4.2Z" />
    </svg>
  );
}
