# `external_schemas/` — DEPRECADO (mayo 2026)

⚠️ **Esta carpeta ya no se usa**. Se mantiene temporalmente como
referencia histórica para facilitar el rollback si fuese necesario,
pero el sistema activo NO la importa.

## Qué había aquí

Copias literales de los modelos ORM de **sv3** (albaranes-persistence-api):

* `orm_models.py`
* `orm_contrato_models.py`
* `orm_contrato_cache_models.py`

El sv7 las usaba para que `Base.metadata.create_all()` pudiera crear
las tablas de sv3 en la BBDD compartida si arrancaba primero contra
una BBDD vacía. La regla de mantenimiento era: "cuando alguien
modifique un ORM en sv3, hay que copiar el cambio aquí también".

## Por qué se ha eliminado del flujo

Esta regla se incumplía. El driver fue el bug `descuento_albaran_aplicado`:

* sv6 añadió esa columna en su ORM y en su DDL.
* sv3 (que también replicaba el DDL del sv6 en `_VALUATION_DDL`) NO se
  actualizó.
* sv7 (que tenía aquí el ORM y en el repo el `_VALUATION_DDL`) tampoco.
* Resultado: la tabla se creaba en BBDD sin la columna y los INSERT
  del sv6 fallaban en runtime.

## Qué se ha hecho en su lugar

Patrón **Schema Contributors**:

1. Cada microservicio que escribe en BBDD expone un `schema_contribution.py`
   con su DDL idempotente y un endpoint público `GET /schema/ddl`.
2. El sv7 al arrancar descubre los contributors configurados (sv3, sv6),
   descarga sus DDL y los aplica en orden topológico contra la BBDD
   compartida. Ver:
   * `application/pipelines/bootstrap_schema_pipeline.py`
   * `application/services/schema_orchestrator.py`
   * `infrastructure/clients/http_schema_ddl_client.py`

Esto elimina la duplicación: ahora cada schema vive en UN solo sitio
(el del servicio dueño). El sv7 ya no necesita actualizarse cuando
sv3/sv6 añaden tablas o columnas.

## Borrado definitivo

Cuando el sistema lleve un par de releases estable con el nuevo flujo,
podemos borrar esta carpeta entera (el `__init__.py` y los tres
`orm_*.py`). Mientras tanto, **no importar nada desde aquí**.
