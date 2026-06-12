# application/utils/apply_patch_to_envelope.py
"""Utilidades para construir el envelope final que sv7 manda a sv3.

NUEVO ENFOQUE — DICIEMBRE 2026:

La fase 2 ahora devuelve el documento corregido COMPLETO
(``documento_revisado``) con el mismo schema que fase 1, no una lista
de cambios estructurados. Por tanto:

  - YA NO hay que "aplicar un patch" sobre los datos de fase 1.
    El ``documento_revisado`` ES directamente el JSON final.
  - La trazabilidad "qué líneas tocó fase 2" se calcula por
    diff entre el JSON original y el revisado.
  - Si fase 2 dice ``review_status="ok"``, el documento revisado es
    idéntico al de fase 1 (lo verificamos). Si dice
    ``"ok_with_changes"`` o ``"inconsistent"``, hay diferencias.

Funciones expuestas:

  - ``extract_documento_revisado(review_envelope)`` →
    extrae el JSON de fase 2 corregido del envelope.

  - ``compute_phase2_line_indices(data_phase_1, data_revised)`` →
    devuelve la lista de índices de líneas modificadas (o nuevas)
    en el documento revisado. Sirve para marcar
    ``source_phase='phase_2'`` en sv3.

  - ``build_review_metadata(...)`` → construye el bloque
    ``review_phase2_metadata`` que sv7 incrusta en el envelope para
    que sv3 lo persista en ``albaran_documents_merge.review_phase2_*``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def extract_documento_revisado(
    review_envelope: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Devuelve ``data.documento_revisado`` del envelope fase 2.

    Si no está presente o tiene tipo incorrecto, devuelve None.
    """
    if not isinstance(review_envelope, dict):
        return None
    data = review_envelope.get("data") or {}
    if not isinstance(data, dict):
        return None
    doc = data.get("documento_revisado")
    if not isinstance(doc, dict):
        return None
    return doc


def apply_sigrid_grounding_overrides(
    *,
    documento: Dict[str, Any],
    grounding,
) -> List[str]:
    """Aplica DETERMINISTICAMENTE las partes validadas del grounding.

    Cinturón y tirantes sobre la fase 2: aunque el prompt le pide a la
    IA que respete los datos validados por CIF/código contra Sigrid, no
    podemos depender de que obedezca. Aquí, DESPUÉS de la fase 2 y
    ANTES de mandar a sv3, sobrescribimos la cabecera con los valores
    canónicos del ERP cuando el grounding los validó:

      - proveedor validado por CIF  → cabecera.proveedor_cif (CIF
        normalizado del ERP) y cabecera.proveedor_nombre (``prv.raz``).
      - obra validada por código    → cabecera.obra_codigo,
        cabecera.obra_nombre y cabecera.obra_direccion canónicos.

    Muta ``documento`` in place y devuelve la lista de campos
    sobrescritos (para log/auditoría). Tolerante a esquema: si no hay
    ``cabecera`` dict, no hace nada.

    ``grounding`` es un HeaderGroundingResult (no se importa el tipo
    para no acoplar utils ↔ ports; se accede por atributos).
    """
    overridden: List[str] = []
    if grounding is None or not isinstance(documento, dict):
        return overridden
    cabecera = documento.get("cabecera")
    if not isinstance(cabecera, dict):
        return overridden

    def _set(field: str, value: Any) -> None:
        if value is None:
            return
        old = cabecera.get(field)
        if old != value:
            cabecera[field] = value
            overridden.append(field)

    if getattr(grounding, "proveedor_validado", False):
        _set("proveedor_cif", getattr(grounding, "proveedor_cif", None))
        _set(
            "proveedor_nombre",
            getattr(grounding, "proveedor_nombre_canonico", None),
        )

    if getattr(grounding, "obra_validada", False):
        _set("obra_codigo", getattr(grounding, "obra_codigo", None))
        _set("obra_nombre", getattr(grounding, "obra_nombre", None))
        _set("obra_direccion", getattr(grounding, "obra_direccion", None))

    if overridden:
        logger.info(
            "[grounding] cabecera sobrescrita con canónicos Sigrid: %s",
            ", ".join(overridden),
        )
    return overridden


def compute_phase2_line_indices(
    *,
    data_phase_1: Dict[str, Any],
    data_revised: Dict[str, Any],
) -> List[int]:
    """Diff entre las líneas de fase 1 y fase 2.

    Devuelve los índices (0-based, sobre ``data_revised.lineas``) de
    las líneas que difieren respecto a fase 1. Esos son los que
    deben marcarse ``source_phase='phase_2'`` en sv3.

    Política:
      - Si el número de líneas cambió: se marcan TODAS las nuevas/
        modificadas. Es la opción más conservadora (no podemos
        casar líneas 1:1 si se han añadido o eliminado).
      - Si el número de líneas es igual: comparamos línea por línea
        por igualdad estructural (después de normalizar None/"").
    """
    lines_1 = (data_phase_1 or {}).get("lineas") or []
    lines_2 = (data_revised or {}).get("lineas") or []

    if not isinstance(lines_1, list) or not isinstance(lines_2, list):
        return []

    if len(lines_1) != len(lines_2):
        # Cambió el número de líneas — todas las del revisado se
        # consideran "phase_2" (no hay forma fiable de aparearlas).
        return list(range(len(lines_2)))

    diffs: List[int] = []
    for idx, (a, b) in enumerate(zip(lines_1, lines_2)):
        if _normalize_line(a) != _normalize_line(b):
            diffs.append(idx)
    return diffs


def _normalize_line(line: Any) -> Any:
    """Normaliza una línea para comparar por igualdad.

    Trata None y "" como equivalentes (la IA puede devolver uno o el
    otro indistintamente y no queremos marcarlo como cambio).
    """
    if not isinstance(line, dict):
        return line
    out: Dict[str, Any] = {}
    for k, v in line.items():
        if v is None or v == "":
            continue
        out[k] = v
    return out


def build_review_metadata(
    *,
    review_envelope: Dict[str, Any],
    phase_2_line_indices: List[int],
) -> Dict[str, Any]:
    """Construye el bloque ``review_phase2_metadata`` para sv3.

    sv3 lo persiste en ``albaran_documents_merge``:
      - review_phase2_status
      - review_phase2_summary
      - review_phase2_changes_count
      - review_phase2_payload_json

    El payload_json contiene la auditoría completa: status, resumen,
    razonamientos y los índices de líneas que cambiaron (calculados
    por sv7 mediante diff).
    """
    data = (review_envelope or {}).get("data") or {}
    razonamientos = data.get("razonamientos") or []
    review_status = data.get("review_status") or ""

    return {
        "review_phase2_status": str(review_status),
        "review_phase2_summary": data.get("explicacion_global"),
        "review_phase2_changes_count": len(razonamientos),
        "review_phase2_payload_json": {
            "review_status": review_status,
            "explicacion_global": data.get("explicacion_global"),
            "razonamientos": razonamientos,
            "phase_2_line_indices": phase_2_line_indices,
            "review_provider": (
                (review_envelope.get("meta") or {}).get("provider")
            ),
            "review_model": (
                (review_envelope.get("meta") or {}).get("model")
            ),
        },
    }


def mark_source_phase(
    *,
    data_revised: Dict[str, Any],
    phase_2_line_indices: List[int],
) -> Dict[str, Any]:
    """Devuelve una copia de ``data_revised`` con cada línea marcada
    con su ``source_phase`` (``'phase_1'`` o ``'phase_2'``).

    Esta marca la lee el Phase2PersistenceService de sv3 después del
    persist principal. Si la línea no existe (no hay ``lineas``), se
    devuelve el dict tal cual.

    NOTA: ``source_phase`` no es un campo del schema
    ``DocumentoAlbaran`` (validado por Pydantic con ``extra='forbid'``).
    Para evitar que sv3 falle, NO lo añadimos directamente al
    documento. Lo metemos en ``review_phase2_metadata`` y dejamos que
    sv3 lo persista por SQL UPDATE en ``albaran_lines_merge``.

    Por tanto, esta función ahora simplemente devuelve
    ``data_revised`` sin tocar (la lógica real de marcado vive en
    sv3 leyendo ``phase_2_line_indices`` del metadata). Se mantiene
    la firma por si en el futuro queremos cambiar el enfoque.
    """
    return data_revised
