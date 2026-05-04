# application/utils/apply_patch_to_envelope.py
"""Aplica el patch de fase 2 sobre el envelope de fase 1.

El envelope de fase 1 tiene la forma:

    {
        "meta": {...},
        "data": {                           # DocumentoAlbaran
            "fecha": "2026-03-09",
            "proveedor_cif": "B12345678",
            ...
            "lines": [
                {"cantidad": 7.5, "precio": 100.0, "importe": 750.0, ...},
                {"cantidad": 8.0, "precio": 50.0,  "importe": 400.0, ...},
                ...
            ]
        },
        "debug": {...}
    }

El patch viene como lista de cambios en formato:

    {
        "campo":          "lines[2].cantidad",
        "valor_anterior": 7.5,
        "valor_propuesto": 8.0,
        "razon":          "...",
        "patron_aplicado": "..."
    }

Esta utilidad:

  1. Aplica los cambios sobre una COPIA del data de fase 1.
  2. Marca cada línea modificada con ``source_phase='phase_2'``.
  3. Devuelve:
        - el data fusionado,
        - los metadatos resumen para guardar en BBDD a nivel
          documento (review_phase2_*).
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Regex para parsear la notación dot/bracket: "lines[2].cantidad"
# ─ se permiten chunks tipo `algo` y `algo[N]`.
_TOKEN_RE = re.compile(r"([A-Za-z_][\w]*)(\[(\d+)\])?")


def parse_path(path: str) -> list[tuple[str, int | None]]:
    """Convierte 'lines[2].cantidad' → [('lines', 2), ('cantidad', None)]."""
    if not path or not isinstance(path, str):
        raise ValueError(f"path inválido: {path!r}")
    tokens: list[tuple[str, int | None]] = []
    for raw_segment in path.split("."):
        m = _TOKEN_RE.fullmatch(raw_segment.strip())
        if not m:
            raise ValueError(
                f"segmento de path no parseable: '{raw_segment}' en '{path}'"
            )
        key = m.group(1)
        index = int(m.group(3)) if m.group(3) is not None else None
        tokens.append((key, index))
    return tokens


def apply_change(target: dict, path: str, new_value: Any) -> bool:
    """Aplica un único cambio. Devuelve True si se modificó algo."""
    tokens = parse_path(path)
    cursor: Any = target

    for i, (key, index) in enumerate(tokens):
        is_last = i == len(tokens) - 1

        # Bajar al hijo `key`.
        if not isinstance(cursor, dict):
            raise ValueError(
                f"cursor no-dict al navegar '{path}' en segmento '{key}'"
            )
        if index is None:
            # campo simple
            if is_last:
                cursor[key] = new_value
                return True
            if key not in cursor or cursor[key] is None:
                cursor[key] = {}
            cursor = cursor[key]
        else:
            # campo array → key[index]
            arr = cursor.get(key)
            if not isinstance(arr, list):
                raise ValueError(
                    f"se esperaba lista en '{key}' al navegar '{path}'"
                )
            if is_last:
                # Último segmento: si new_value es None → eliminar.
                if new_value is None:
                    if 0 <= index < len(arr):
                        arr.pop(index)
                        return True
                    return False
                # Si index == len(arr) → append (línea nueva).
                if index == len(arr):
                    arr.append(new_value)
                    return True
                # Reemplazo dentro del rango.
                if 0 <= index < len(arr):
                    arr[index] = new_value
                    return True
                raise IndexError(
                    f"índice {index} fuera de rango (len={len(arr)}) "
                    f"en '{path}'"
                )
            # No es último: navegar al elemento del array.
            if 0 <= index < len(arr):
                cursor = arr[index]
            else:
                raise IndexError(
                    f"índice {index} fuera de rango (len={len(arr)}) "
                    f"en '{path}'"
                )
    return False


def _line_index_from_path(path: str) -> int | None:
    """Si el path empieza por 'lines[N]', devuelve N. Si no, None."""
    m = re.match(r"^\s*lines\[(\d+)\]", path or "")
    return int(m.group(1)) if m else None


def apply_patch_to_phase1_data(
    phase_1_data: dict,
    cambios: list[dict],
) -> tuple[dict, dict]:
    """Aplica todos los cambios y marca las líneas afectadas.

    Devuelve una tupla ``(merged_data, summary)``:

      - ``merged_data``: copia profunda de ``phase_1_data`` con los
        cambios aplicados. Las líneas modificadas o introducidas
        nuevas tienen un campo ``source_phase='phase_2'``.
      - ``summary``: dict con métricas para sv7/sv3:
            {
                "applied_count": int,
                "rejected_count": int,
                "rejected_details": [{"campo": "...", "error": "..."}],
                "lines_phase2_count": int,
                "lines_phase2_indices": [...]
            }
    """
    merged = copy.deepcopy(phase_1_data)

    # Inicializamos source_phase='phase_1' para todas las líneas (los
    # cambios sobreescribirán a 'phase_2' donde toque).
    if isinstance(merged.get("lines"), list):
        for line in merged["lines"]:
            if isinstance(line, dict) and "source_phase" not in line:
                line["source_phase"] = "phase_1"

    applied = 0
    rejected: list[dict] = []
    touched_line_indices: set[int] = set()

    for cambio in cambios or []:
        if not isinstance(cambio, dict):
            rejected.append({
                "campo": "?",
                "error": "cambio no es dict",
            })
            continue
        path = cambio.get("campo")
        new_value = cambio.get("valor_propuesto")

        # Línea afectada (si aplica) — la calculamos antes para saber
        # qué marcar como phase_2 si el cambio se aplica con éxito.
        line_idx = _line_index_from_path(path) if isinstance(path, str) else None

        try:
            changed = apply_change(merged, path, new_value)
        except Exception as exc:
            rejected.append({"campo": path or "?", "error": str(exc)})
            logger.warning(
                "[apply_patch] cambio rechazado: campo=%r error=%s",
                path, exc,
            )
            continue

        if not changed:
            rejected.append({
                "campo": path or "?",
                "error": "no se aplicó ningún cambio",
            })
            continue

        applied += 1
        if line_idx is not None:
            touched_line_indices.add(line_idx)

    # Tras aplicar cambios: marcar las líneas tocadas con phase_2.
    if isinstance(merged.get("lines"), list):
        for idx in sorted(touched_line_indices):
            if 0 <= idx < len(merged["lines"]):
                line = merged["lines"][idx]
                if isinstance(line, dict):
                    line["source_phase"] = "phase_2"

    summary = {
        "applied_count": applied,
        "rejected_count": len(rejected),
        "rejected_details": rejected,
        "lines_phase2_count": len(touched_line_indices),
        "lines_phase2_indices": sorted(touched_line_indices),
    }
    return merged, summary


def build_review_metadata(
    review_envelope: dict,
    apply_summary: dict,
) -> dict:
    """Construye el bloque review_phase2_* que se le pasa a sv3.

    sv3 lo persiste en albaran_documents_merge:
      - review_phase2_status
      - review_phase2_summary
      - review_phase2_changes_count
      - review_phase2_payload_json (JSON con la lista completa de cambios)

    También conserva el resumen del apply (cuántos se aplicaron de
    verdad vs cuántos rechazó la utilidad por path inválido, etc.).
    """
    data = review_envelope.get("data") or {}
    cambios = data.get("cambios") or []
    return {
        "review_phase2_status": str(data.get("review_status") or ""),
        "review_phase2_summary": data.get("explicacion_global"),
        "review_phase2_changes_count": len(cambios),
        "review_phase2_payload_json": {
            "review_status": data.get("review_status"),
            "explicacion_global": data.get("explicacion_global"),
            "cambios": cambios,
            "apply_summary": apply_summary,
            "provider": (review_envelope.get("meta") or {}).get("provider"),
            "model": (review_envelope.get("meta") or {}).get("model"),
        },
    }
