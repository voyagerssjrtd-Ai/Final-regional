# The file contains only application logic, SQL generation, CRUD operations logic


"""Chat-to-SQL + Catalog (RAG) endpoints (initial scaffold).

Phase 1 goals:
- /catalog/rebuild : extract DB metadata (tables, columns, FKs) and (placeholder) build docs.
- /chat : accept user prompt, return stub JSON with answer + empty sql + placeholders for retrieval context.

Integration points left as TODO:
- Azure Cognitive Search index upsert/query
- Embeddings via Azure OpenAI (text-embedding-3-large)
- Chat model (gpt-4.1 deployment) for SQL generation

NOTE: This is a non-functional scaffold; implement incremental pieces safely.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Depends
from fastapi.responses import StreamingResponse
from typing import Dict, Any, List, Tuple, Iterator
import time
import re
import logging
import uuid
import os
import json
try:  # pragma: no cover
    import pyodbc
    _HAS_PYODBC = True
except Exception:  # pragma: no cover
    _HAS_PYODBC = False

router = APIRouter(
    prefix="/chat-sql",
    tags=["chat-sql"],
    responses={404: {"description": "Not found"}},
)

# In-memory ephemeral stores (replace with persistent later)
CATALOG_STATE: Dict[str, Any] = {"status": "idle"}
CHAT_SESSIONS: Dict[str, List[Dict[str, Any]]] = {}
LAST_PREVIEWS: Dict[str, Dict[str, Any]] = {}  # session_id -> {table, columns}
GLOBAL_LAST_PREVIEW_KEY = '__global__'
UPDATE_HISTORY: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> list of update ops (stack)
PAGE_STATE: Dict[str, Dict[str, Any]] = {}  # session_id -> paging context
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'chat_sessions')
os.makedirs(SESSIONS_DIR, exist_ok=True)

logger = logging.getLogger("chat_sql")

FORBIDDEN_SQL = re.compile(r"\b(DROP|DELETE|UPDATE|TRUNCATE|ALTER|MERGE|INSERT)\b", re.IGNORECASE)

# Friendly mode toggle (adds short explanation + emoji). Could be wired to config later.
FRIENDLY_MODE = True
SUPPORTED_LANGS = {"en","es","fr","de","it"}

_I18N_PREFIXES = {
    'preview': {
        'en': '📊 Data preview',
        'es': '📊 Vista previa de datos',
        'fr': '📊 Aperçu des données',
        'de': '📊 Datenvorschau',
        'it': '📊 Anteprima dati'
    },
    'update': {
        'en': '✅ Update', 'es': '✅ Actualización', 'fr': '✅ Mise à jour', 'de': '✅ Aktualisierung', 'it': '✅ Aggiornamento'
    },
    'contextual_update': {
        'en': '✅ Value changed','es':'✅ Valor cambiado','fr':'✅ Valeur modifiée','de':'✅ Wert geändert','it':'✅ Valore modificato'
    },
    'multi_update': {
        'en':'✅ Multi-column update','es':'✅ Actualización multicolumna','fr':'✅ Mise à jour multi-colonnes','de':'✅ Mehrspaltiges Update','it':'✅ Aggiornamento multi-colonna'
    },
    'insert': {
        'en':'➕ Inserted','es':'➕ Insertado','fr':'➕ Inséré','de':'➕ Eingefügt','it':'➕ Inserito'
    },
    'revert': {
        'en':'↩️ Reverted','es':'↩️ Revertido','fr':'↩️ Rétabli','de':'↩️ Rückgängig','it':'↩️ Ripristinato'
    },
    'mass_pending': {
        'en':'🧐 Mass update pending confirmation','es':'🧐 Actualización masiva pendiente','fr':'🧐 Mise à jour massive en attente','de':'🧐 Massenänderung wartet auf Bestätigung','it':'🧐 Aggiornamento di massa in attesa'
    },
    'mass_confirm': {
        'en':'✅ Mass update applied','es':'✅ Actualización masiva aplicada','fr':'✅ Mise à jour massive appliquée','de':'✅ Massenänderung angewendet','it':'✅ Aggiornamento di massa applicato'
    },
    'history': {
        'en':'📜 Recent changes','es':'📜 Cambios recientes','fr':'📜 Modifications récentes','de':'📜 Letzte Änderungen','it':'📜 Modifiche recenti'
    },
    'soft_delete_pending': {
        'en':'💤 Soft delete pending confirmation','es':'💤 Borrado lógico pendiente','fr':'💤 Suppression logique en attente','de':'💤 Logisches Löschen ausstehend','it':'💤 Cancellazione logica in attesa'
    },
    'restore_pending': {
        'en':'♻️ Restore pending confirmation','es':'♻️ Restauración pendiente','fr':'♻️ Restauration en attente','de':'♻️ Wiederherstellung ausstehend','it':'♻️ Ripristino in attesa'
    },
    'restore_applied': {
        'en':'♻️ Restored','es':'♻️ Restaurado','fr':'♻️ Restauré','de':'♻️ Wiederhergestellt','it':'♻️ Ripristinato'
    },
    'metrics': {
        'en':'📈 Metrics','es':'📈 Métricas','fr':'📈 Métriques','de':'📈 Metriken','it':'📈 Metriche'
    }
}

def _friendly_wrap(base: str, kind: str | None = None, lang: str = 'en') -> str:
    if not FRIENDLY_MODE or not base:
        return base
    if lang not in SUPPORTED_LANGS:
        lang = 'en'
    if base.strip().startswith(('✅','📊','↩️','📝','🧐','➕','♻️','💤','📜')):
        return base
    if not kind:
        return base
    prefix_map = _I18N_PREFIXES.get(kind)
    if not prefix_map:
        return base
    prefix = prefix_map.get(lang) or prefix_map.get('en')
    if not prefix:
        return base
    return f"{prefix}\n{base}" if '\n' in base else f"{prefix}: {base}"

# --- Multilingual & fuzzy intent normalization configuration ---
FUZZY_INTENT_DISTANCE = int(os.getenv('INTENT_FUZZY_DISTANCE', '2'))

INTENT_SYNONYMS = {
    'update': {
        'update','change','modify','replace','actualizar','cambiar','modificar','mettre','changer','aktualisieren','ändern','bearbeiten','aggiorna','modifica','mettez','mise','giorna'
    },
    'insert': {
        'add','insert','append','create','añadir','agregar','insertar','ajouter','inserer','hinzufügen','einfügen','aggiungi','inserisci'
    },
    'revert': {
        'revert','undo','rollback','deshacer','anular','restaurar','annuler','revenir','zurücksetzen','widerrufen','ripristina','ripristinare'
    }
}

# Paging & extended operations regexes
PAGING_NEXT_REGEX = re.compile(r"^(?:show\s+)?(?:next|more)(?:\s+(\d+))?\s+rows?", re.IGNORECASE)
PAGING_PREV_REGEX = re.compile(r"^(?:show\s+)?(?:previous|prev|back)(?:\s+(\d+))?\s+rows?", re.IGNORECASE)

def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a)-len(b)) > FUZZY_INTENT_DISTANCE:
        # early bail if lengths far apart
        return FUZZY_INTENT_DISTANCE + 1
    dp = list(range(len(b)+1))
    for i, ca in enumerate(a, 1):
        prev = dp[0]
        dp[0] = i
        for j, cb in enumerate(b, 1):
            cur = dp[j]
            if ca == cb:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j-1])
            prev = cur
    return dp[-1]

def _normalize_intent_verbs(text: str) -> str:
    if not text:
        return text
    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9_']+", text)
    # Build lower mapping of token -> canonical if match or fuzzy
    normalized_tokens: List[str] = []
    for tok in tokens:
        low = tok.lower()
        mapped = None
        for canon, syns in INTENT_SYNONYMS.items():
            if low in syns:
                mapped = canon
                break
        if not mapped:
            # fuzzy search which set
            for canon, syns in INTENT_SYNONYMS.items():
                for s in syns:
                    dist = _edit_distance(low, s)
                    # Stricter fuzzy rule: allow distance 1 always; distance 2 only if token length >=6
                    if dist <= 1 or (dist == 2 and len(low) >= 6 and len(s) >= 6):
                        mapped = canon
                        break
                if mapped:
                    break
        normalized_tokens.append(mapped if mapped else tok)
    # Reconstruct by replacing only whole word occurrences in original string
    # Simple approach: iterate words again, substitute sequentially
    it = iter(normalized_tokens)
    def repl(m):
        return next(it)
    return re.sub(r"[A-Za-zÀ-ÿ0-9_']+", repl, text)

# --- Metrics helpers (row / column counts) ---
ROW_COUNT_TRIGGER = re.compile(r"(rows?\s+in\s+each\s+table|each\s+table\s+rows?|row\s+counts?|number\s+of\s+rows.*each|list\s+all\s+tables.*rows)", re.IGNORECASE)
COLUMN_COUNT_TRIGGER = re.compile(r"(columns?\s+in\s+each\s+table|each\s+table\s+columns?|column\s+counts?|number\s+of\s+columns.*each)", re.IGNORECASE)
LARGEST_TABLE_TRIGGER = re.compile(
    r"("
    r"which\s+(?:table|one)\s+has\s+(?:the\s+)?most\s+rows(?:\s+and\s+columns)?|"
    r"which\s+(?:table|one)\s+has\s+more\s+rows|"
    r"which\s+(?:table|one)\s+has\s+more\s+columns|"
    r"which\s+(?:table|one)\s+has\s+the\s+most\s+columns|"
    r"largest\s+table\s+by\s+rows|"
    r"most\s+rows\s+table|"
    r"most\s+columns\s+table"
    r")",
    re.IGNORECASE,
)
SIZE_TRIGGER = re.compile(r"(table\s+sizes?|data\s+size|which\s+table\s+(has|is)\s+(the\s+)?largest\s+by\s+size|largest\s+table\s+by\s+size|has\s+more\s+size|more\s+size|bigger\s+size)", re.IGNORECASE)
BIG_TABLE_TRIGGER = re.compile(r"(biggest\s+table|which\s+table\s+is\s+biggest|which\s+table\s+is\s+big|largest\s+table|which\s+is\s+the\s+largest\s+table)", re.IGNORECASE)
MORE_DATA_TRIGGER = re.compile(r"(which\s+table\s+has\s+more\s+data|which\s+table\s+has\s+the\s+most\s+data|most\s+data\s+table|more\s+data\s+table)", re.IGNORECASE)

def _compute_row_counts(app_config: AppConfiguration, limit_tables: int = 200):
    cn = _connect_db(app_config)
    headers = ["table","rows"]
    rows: List[Tuple[Any,...]] = []
    try:
        cur = cn.cursor()
        tbls = (CATALOG_STATE.get('table_names') or [])
        if not tbls:
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'")
            tbls = [f"{r[0]}.{r[1]}" for r in cur.fetchall()]
        for t in tbls[:limit_tables]:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                c = cur.fetchone()
                rows.append((t, c[0] if c else 0))
            except Exception as e:  # continue on per-table failure
                rows.append((t, f"error: {e}"))
    finally:
        try:
            cn.close()
        except Exception:
            pass
    return headers, rows

def _compute_column_counts(app_config: AppConfiguration, limit_tables: int = 200):
    cn = _connect_db(app_config)
    headers = ["table","columns"]
    rows: List[Tuple[Any,...]] = []
    try:
        cur = cn.cursor()
        cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, COUNT(*) AS col_count FROM INFORMATION_SCHEMA.COLUMNS GROUP BY TABLE_SCHEMA, TABLE_NAME ORDER BY TABLE_SCHEMA, TABLE_NAME")
        for r in cur.fetchall()[:limit_tables]:
            rows.append((f"{r[0]}.{r[1]}", r[2]))
    finally:
        try:
            cn.close()
        except Exception:
            pass
    return headers, rows

def _compute_row_and_column_counts(app_config: AppConfiguration, limit_tables: int = 200):
    """Return combined list: table, rows, columns. Rows fetched individually (may be slower)."""
    # Get columns first (single query), then row counts.
    h_cols, rows_cols = _compute_column_counts(app_config, limit_tables)
    col_map_counts = {t: c for t, c in rows_cols}
    h_rows, rows_row_counts = _compute_row_counts(app_config, limit_tables)
    row_map_counts = {t: c for t, c in rows_row_counts if isinstance(c,(int,float))}
    combined = []
    for t in sorted(set(col_map_counts.keys()) | set(row_map_counts.keys())):
        combined.append((t, row_map_counts.get(t, 'n/a'), col_map_counts.get(t, 'n/a')))
    # Sort by rows desc (numeric only to top), keep others after.
    def sort_key(r):
        rv = r[1]
        return (-rv if isinstance(rv,(int,float)) else 0, r[0])
    combined.sort(key=sort_key)
    return ["table","rows","columns"], combined[:limit_tables]

def _compute_table_sizes(app_config: AppConfiguration, limit_tables: int = 200):
    """Compute approximate size (MB) per table using allocation metadata. Falls back silently on error."""
    cn = _connect_db(app_config)
    rows: List[Tuple[Any,...]] = []
    try:
        cur = cn.cursor()
        # Using allocation units for approximate size; 8KB pages -> MB
        size_sql = (
            "SELECT TOP (@lim) s.name + '.' + t.name AS table_name, "
            "CAST(SUM(a.used_pages)*8.0/1024 AS DECIMAL(18,2)) AS size_mb "
            "FROM sys.tables t "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "JOIN sys.indexes i ON t.object_id = i.object_id "
            "JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id "
            "JOIN sys.allocation_units a ON p.partition_id = a.container_id "
            "GROUP BY s.name, t.name ORDER BY size_mb DESC" )
        try:
            cur.execute(size_sql, lim=limit_tables)
        except Exception:
            # Some Synapse flavors may not allow parameter for TOP; fallback literal
            cur.execute(size_sql.replace('TOP (@lim)', f'TOP ({limit_tables})'))
        for r in cur.fetchall():
            rows.append((r[0], r[1]))
    except Exception as e:
        rows.append(("error", f"size_failed: {e}"))
    finally:
        try:
            cn.close()
        except Exception:
            pass
    return ["table","size_mb"], rows

def _compute_table_overview(app_config: AppConfiguration, limit_tables: int = 200):
    """Return combined overview: table | rows | columns | size_mb; sorted by rows desc."""
    h_rc, rc = _compute_row_and_column_counts(app_config, limit_tables)
    h_sz, sz = _compute_table_sizes(app_config, limit_tables)
    size_map = {t: s for t, s in sz if isinstance(s,(int,float,))}
    merged = []
    for t, r, c in rc:
        merged.append((t, r, c, size_map.get(t, 'n/a')))
    def sort_key(v):
        rv = v[1]
        return (-rv if isinstance(rv,(int,float)) else 0, v[0])
    merged.sort(key=sort_key)
    return ["table","rows","columns","size_mb"], merged[:limit_tables]

def _side_by_side_preview(tables: List[str], app_config: AppConfiguration, max_rows: int = 10) -> str:
    """Return a textual side-by-side (stacked sections) preview of up to two tables.

    We show each table separately (stacked) to avoid excessive horizontal wrapping, explicitly listing columns.
    """
    if not tables:
        return '(no tables specified)'
    use_tables = tables[:2]
    col_map = CATALOG_STATE.get('columns_by_table') or {}
    sections: List[str] = []
    try:
        conn = _connect_db(app_config)
    except Exception as e:
        return f"DB connection failed: {e}"
    try:
        cur = conn.cursor()
        misses: Dict[str, List[str]] = CATALOG_STATE.get('last_fuzzy_misses', {}) if isinstance(CATALOG_STATE.get('last_fuzzy_misses'), dict) else {}
        for t in use_tables:
            t_norm = t.lower()
            # try to find fully qualified name in catalog if user only gave short name
            if t_norm not in col_map:
                for k in col_map.keys():
                    if k.endswith('.'+t_norm.split('.')[-1]):
                        t_norm = k
                        break
            cols = col_map.get(t_norm, [])
            # extract column names if catalog entries include type hints (col:type)
            col_names = [c.split(':',1)[0] for c in cols][:12]  # cap columns to avoid wide output
            col_list_sql = '*'
            if col_names:
                bracketed = ', '.join(f"[{c}]" for c in col_names)
                col_list_sql = bracketed
            safe_table = t if '.' in t else f"dbo.{t}"
            # If original token had no match, show suggestions instead of querying invalid object
            base_tok = t.split('.')[-1].lower()
            if t not in col_map and t_norm not in col_map and base_tok in (misses.keys()):
                suggs = misses.get(base_tok) or misses.get(t) or []
                if suggs:
                    sections.append(f"=== {t} (no match) ===\nDid you mean: \n - " + "\n - ".join(suggs))
                    continue
                else:
                    sections.append(f"=== {t} (no match) ===\n(No close table suggestions)")
                    continue
            sql = f"SELECT TOP {max_rows} {col_list_sql} FROM {safe_table}"
            try:
                cur.execute(sql)
                headers = [d[0] for d in cur.description]
                rows = cur.fetchall()
                preview = _format_preview(headers, rows, max_rows=max_rows)
                sections.append(f"=== {safe_table} (TOP {max_rows}) ===\n{preview}")
            except Exception as inner_e:
                sections.append(f"=== {safe_table} ===\n(error executing preview: {inner_e})")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return '\n\n'.join(sections)



def _sanitize_view_name(name: str) -> str:
    cleaned = name.strip().rstrip(';')
    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', cleaned)
    return cleaned or 'generated_view'

def _connect_db(cfg: AppConfiguration):  # enhanced env specific
    if not _HAS_PYODBC:
        raise RuntimeError("pyodbc not installed")
    endpoint = (cfg.synapse_sql_endpoint or '').split()[0]
    if not endpoint:
        raise RuntimeError("synapse_sql_endpoint not configured")
    if cfg.synapse_sql_odbc_connstr:
        try:
            return pyodbc.connect(cfg.synapse_sql_odbc_connstr)
        except Exception as e:
            raise RuntimeError(f"direct connection failed: {e}")
    try:
        from azure.identity import DefaultAzureCredential
        cred = DefaultAzureCredential()
        token = cred.get_token("https://database.windows.net/.default").token
    except Exception as e:
        raise RuntimeError(f"aad token acquisition failed: {e}")
    access_token = token.encode('utf-16-le')
    conn_str = (
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{endpoint},1433;Database={cfg.synapse_db_name};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;"
    )
    try:
        cn = pyodbc.connect(conn_str, attrs_before={1256: access_token})
    except Exception as primary_e:
        # fallback packed token
        try:
            import struct as _struct
            packed = _struct.pack("=i", len(access_token)) + access_token
            cn = pyodbc.connect(conn_str, attrs_before={1256: packed})
        except Exception as retry_e:
            raise RuntimeError(f"odbc connect failed primary={primary_e}; retry={retry_e}")
    # sanity
    try:
        cur = cn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
    except Exception:
        try:
            cn.close()
        except Exception:
            pass
        raise
    return cn

def _format_preview(headers: List[str], rows: List[Tuple[Any, ...]], max_col_width: int = 40, max_rows: int = 20, max_total_chars: int = 2000) -> str:
    if not rows:
        return '(no rows)'
    rows = rows[:max_rows]
    widths = []
    for i, h in enumerate(headers):
        col_vals = [h] + [str(r[i]) if r[i] is not None else 'NULL' for r in rows]
        w = max(len(v) for v in col_vals)
        w = min(w, max_col_width)
        widths.append(w)
    def trunc(v, w):
        s = 'NULL' if v is None else str(v)
        if len(s) > w:
            return s[: max(0, w-1)] + '…'
        return s
    header_line = ' | '.join(trunc(h, widths[i]).ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = '-+-'.join('-'*widths[i] for i in range(len(headers)))
    body_lines = []
    for r in rows:
        body_lines.append(' | '.join(trunc(r[i], widths[i]).ljust(widths[i]) for i in range(len(headers))))
    out = '\n'.join([header_line, sep_line] + body_lines)
    if len(out) > max_total_chars:
        out = out[: max_total_chars-5] + '\n...'
    return out

def _record_page_state(session_id: str, table: str, key_col: str, page_size: int, order: str, last_offset: int):
    PAGE_STATE[session_id] = {"table": table, "key_col": key_col, "page_size": page_size, "order": order, "last_offset": last_offset}

def _attempt_paging(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    lower = text.lower().strip()
    m_next = PAGING_NEXT_REGEX.search(lower)
    m_prev = PAGING_PREV_REGEX.search(lower)
    if lower in ("more", "next", "next page"):
        m_next = True  # sentinel flag meaning next page
    if lower in ("prev", "previous", "back"):
        m_prev = True
    if not (m_next or m_prev):
        return None
    state = PAGE_STATE.get(session_id)
    if not state:
        return {"error": "no paging context"}
    table = state["table"]
    key_col = state["key_col"]
    page_size = state["page_size"]
    order = state["order"]
    if isinstance(m_next, re.Match) and m_next.group(1):
        page_size = max(1, min(500, int(m_next.group(1))))
    if isinstance(m_prev, re.Match) and m_prev.group(1):
        page_size = max(1, min(500, int(m_prev.group(1))))
    offset = state["last_offset"]
    if m_prev and offset > 0:
        offset = max(0, offset - page_size)
    elif m_next:
        offset = offset + page_size
    cols = CATALOG_STATE.get('columns_by_table', {}).get(table.lower(), [])
    col_names = [c.split(':',1)[0] for c in cols][:25] or ['*']
    select_list = ', '.join(f'[{c}]' for c in col_names)
    sql = f"SELECT {select_list} FROM {table} ORDER BY [{key_col}] {order} OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY"
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute(sql)
        hdrs = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
        _record_page_state(session_id, table, key_col, page_size, order, offset)
        return {"preview": _format_preview(hdrs, rows, max_rows=page_size), "sql": sql, "page": {"offset": offset, "size": page_size}}
    except Exception as e:
        return {"error": f"paging_failed: {e}"}

def _should_preview(user_text: str) -> bool:
    """Heuristic to decide if user wants a data preview.

    Expanded to handle broader natural language (misspellings, synonyms, imperative forms):
      Examples triggering preview:
        - "give all contents of frs_transactionmd table"
        - "fetch the data from ..."
        - "return rows for ..."
        - "list out ..."
        - Misspellings like 'contnts', 'dsiplay'.
    """
    if not user_text:
        return False
    t = user_text.lower()
    # Direct SQL wildcard
    if 'select * from' in t:
        return True
    # Core trigger roots and synonyms
    trigger_roots = {
        'show','display','list','view','see','print','get','give','fetch','return','provide','retrieve','pull'
    }
    # Additional phrases
    phrase_triggers = [
        'give all', 'all contents', 'entire table', 'full table', 'complete table', 'list out', 'show me', 'show full', 'show entire', 'show complete'
    ]
    for p in phrase_triggers:
        if p in t:
            return True
    # Misspellings map (common typos -> canonical)
    misspellings = ['dsiplay','dispaly','contnts','contnt','datta','recrods']
    # Quick fuzzy: edit distance <=2 vs triggers
    def edit_dist(a: str, b: str) -> int:
        if abs(len(a)-len(b)) > 2:
            return 3
        dp = list(range(len(b)+1))
        for i, ca in enumerate(a, 1):
            prev = dp[0]
            dp[0] = i
            for j, cb in enumerate(b, 1):
                cur = dp[j]
                if ca == cb:
                    dp[j] = prev
                else:
                    dp[j] = 1 + min(prev, dp[j], dp[j-1])
                prev = cur
        return dp[-1]
    tokens = re.findall(r"[a-z0-9_]+", t)
    has_trigger = any(tok in trigger_roots for tok in tokens)
    if not has_trigger:
        # fuzzy against trigger roots & known misspellings
        for tok in tokens:
            for root in trigger_roots.union(set(misspellings)):
                if edit_dist(tok, root) <= 2:
                    has_trigger = True
                    break
            if has_trigger:
                break
    content_markers = {'table','rows','data','contents','records','dataset'}
    has_content_word = any(c in t for c in content_markers) or any(tok in content_markers for tok in tokens)
    # If phrase like "give all <table>" consider implied content
    if not has_content_word and re.search(r"give\s+all\s+[a-z0-9_]+", t):
        has_content_word = True
    # Table mention heuristic: word followed by 'table'
    if ' table' in t and not has_content_word:
        has_content_word = True
    return has_trigger and has_content_word

def _safe_execute_select_preview(sql_candidate: str, app_config: AppConfiguration) -> Tuple[str, str] | Tuple[None, None]:
    """If sql_candidate looks like a simple SELECT (no joins/where heavy) execute and return preview."""
    if not sql_candidate:
        return (None, None)
    sql = sql_candidate.strip().rstrip(';')
    upper = sql.upper()
    if not upper.startswith('SELECT'):
        return (None, None)
    # basic safety: forbid update/delete etc
    if any(k in upper for k in ['UPDATE ', 'DELETE ', 'INSERT ', ' MERGE ', ' DROP ', ' ALTER ']):
        return (None, None)
    # limit rows if not already limited
    if ' TOP ' not in upper.split('\n',1)[0]:
        # inject TOP 20 after SELECT
        sql = re.sub(r'^SELECT\s+', 'SELECT TOP 20 ', sql, flags=re.IGNORECASE)
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute(sql)
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return (_format_preview(headers, rows), sql+';')
    except Exception:
        return (None, None)

ADD_FIRST_REGEXES = [
    re.compile(r"add\s+'([^']+)'\s+as\s+value\s+to\s+the\s+first\s+column", re.IGNORECASE),
    re.compile(r'add\s+"([^"]+)"\s+as\s+value\s+to\s+the\s+first\s+column', re.IGNORECASE),
    re.compile(r"add\s+'([^']+)'\s+to\s+the\s+first\s+column", re.IGNORECASE),
    re.compile(r'add\s+"([^"]+)"\s+to\s+the\s+first\s+column', re.IGNORECASE),
    re.compile(r"add\s+([^\s]+)\s+to\s+the\s+first\s+column", re.IGNORECASE),
    re.compile(r"add\s+'([^']+)'\s+as\s+first\s+column", re.IGNORECASE),
]

def _parse_add_first_column(text: str) -> str | None:
    t = text.strip()
    lower = t.lower()
    if 'add' not in lower or 'first column' not in lower:
        return None
    for rx in ADD_FIRST_REGEXES:
        m = rx.search(t)
        if m:
            val = m.group(1)
            return val.strip().strip("'\"")
    # simple fallback pattern: add <word> first column
    m2 = re.search(r"add\s+([A-Za-z0-9_]+)\s+.*first\s+column", t, re.IGNORECASE)
    if m2:
        return m2.group(1)
    return None

def _execute_insert_first_column(session_id: str, value: str, app_config: AppConfiguration) -> Dict[str, Any]:
    ctx = LAST_PREVIEWS.get(session_id)
    if not ctx:
        return {'error': 'No previous table context'}
    table = ctx.get('table')
    columns = ctx.get('columns') or []
    if not table or not columns:
        return {'error': 'Incomplete preview context'}
    first_col = columns[0]
    # Fetch full schema to ensure nullability
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        schema, tbl = (table.split('.',1) if '.' in table else ('dbo', table))
        cur.execute("""SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION""", (schema, tbl))
        schema_rows = cur.fetchall()
        col_meta = [(r[0], r[1], r[2], r[3]) for r in schema_rows]
        # Determine which columns we can omit (nullable or default)
        required_others = [c for c in col_meta if c[0] != first_col and c[1] == 'NO' and c[2] is None]
        if required_others:
            needed = ", ".join(r[0] for r in required_others)
            return {'error': f'Cannot insert only first column; other NOT NULL columns without defaults: {needed}', 'hint': f"Provide values, e.g. add '<value>' to the first column with {required_others[0][0]}=<val>"}
        # Build insert using only first column (others default/null)
        insert_sql = f"INSERT INTO {table} ([{first_col}]) VALUES (?)"
        cur.execute(insert_sql, (value,))
        affected = cur.rowcount
        if affected != 1:
            conn.rollback()
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return {'error': f'Insert affected {affected} rows'}
        # Return the inserted row (best-effort by ordering desc on identity or first column)
        try:
            cur.execute(f"SELECT TOP 1 * FROM {table} ORDER BY [{first_col}] DESC")
            row = cur.fetchone()
            headers = [d[0] for d in cur.description]
            inserted = {h: (row[i] if row else None) for i,h in enumerate(headers)}
        except Exception:
            inserted = {}
        conn.commit()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return {'inserted': True, 'table': table, 'column': first_col, 'value': value, 'sql': insert_sql, 'row': inserted}
    except Exception as e:
        msg = str(e)
        if 'Conversion failed when converting date' in msg:
            # provide schema hint
            try:
                if 'schema_rows' in locals():
                    schema_info = [{ 'column': r[0], 'nullable': r[1], 'default': r[2], 'type': r[3]} for r in schema_rows]
                else:
                    schema_info = []
            except Exception:
                schema_info = []
            return {'error': msg, 'schema': schema_info, 'hint': 'First column appears to be a date/datetime; supply value like 2025-10-08 or 2025-10-08T00:00:00'}
        return {'error': msg}

def _parse_add_first_column_with_extras(text: str):
    """Parse commands that attempt to insert first column plus additional required columns.

    Examples:
      add 'EPR' to the first column with GLOBAL_ID=123
      add "EPR" as value to the first column and GLOBAL_ID=123, OTHER_COL='X'
      add EPR to the first column with GLOBAL_ID 123
    Returns tuple (first_value, extras_dict) or None.
    """
    if 'first column' not in text.lower():
        return None
    # capture the primary value using existing regexes first
    base_val = _parse_add_first_column(text)
    if not base_val:
        # attempt looser pattern: add <token> first column with ...
        m = re.search(r"add\s+([A-Za-z0-9_\-]+)\s+.*first\s+column", text, re.IGNORECASE)
        if m:
            base_val = m.group(1)
    if not base_val:
        return None
    # find extras portion after ' with ' or ' and ' that contains '=' or plausible key value pairs
    m_with = re.search(r"(?:with|and)\s+(.+)$", text, re.IGNORECASE)
    if not m_with:
        return None
    tail = m_with.group(1).strip()
    # split on commas first
    parts = re.split(r",| and ", tail, flags=re.IGNORECASE)
    extras: Dict[str, Any] = {}
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # key=value form
        m_eq = re.match(r"([A-Za-z0-9_]+)\s*=\s*(.+)", p)
        if m_eq:
            k, v = m_eq.groups()
        else:
            # key value (space) form (only if two tokens)
            m_space = re.match(r"([A-Za-z0-9_]+)\s+([^=\s]+)$", p)
            if not m_space:
                continue
            k, v = m_space.groups()
        v = v.strip().strip(',')
        v = v.strip()
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        # primitive inference
        if re.fullmatch(r"-?\d+", v):
            val: Any = int(v)
        elif re.fullmatch(r"-?\d+\.\d+", v):
            try:
                val = float(v)
            except Exception:
                val = v
        elif v.lower() in ('null','none'):
            val = None
        else:
            val = v
        extras[k] = val
    if not extras:
        return None
    return base_val, extras

def _execute_insert_first_column_with_extras(session_id: str, first_value: Any, extras: Dict[str, Any], app_config: AppConfiguration) -> Dict[str, Any]:
    ctx = LAST_PREVIEWS.get(session_id)
    if not ctx:
        # fallback to global preview context if available
        ctx = LAST_PREVIEWS.get(GLOBAL_LAST_PREVIEW_KEY)
    if not ctx:
        return {'error': 'No previous table context'}
    table = ctx.get('table')
    columns = ctx.get('columns') or []
    if not table or not columns:
        return {'error': 'Incomplete preview context'}
    first_col = columns[0]
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        schema, tbl = (table.split('.',1) if '.' in table else ('dbo', table))
        cur.execute("""SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION""", (schema, tbl))
        schema_rows = cur.fetchall()
        col_meta = [(r[0], r[1], r[2], r[3]) for r in schema_rows]
        valid_cols = {c[0] for c in col_meta}
        # Validate extras columns
        bad = [k for k in extras.keys() if k not in valid_cols]
        if bad:
            return {'error': f'Unknown columns: {", ".join(bad)}'}
        if first_col in extras:
            return {'error': f'Do not repeat first column {first_col} in extras'}
        required_others = [c for c in col_meta if c[0] != first_col and c[1] == 'NO' and c[2] is None]
        missing = [c[0] for c in required_others if c[0] not in extras]
        if missing:
            return {'error': f'Missing required NOT NULL columns: {", ".join(missing)}'}
        # Build ordered column/value arrays
        insert_cols = [first_col] + list(extras.keys())
        params = [first_value] + [extras[k] for k in extras.keys()]
        col_list_sql = ', '.join(f'[{c}]' for c in insert_cols)
        placeholders = ', '.join('?' for _ in insert_cols)
        insert_sql = f"INSERT INTO {table} ({col_list_sql}) VALUES ({placeholders})"
        cur.execute(insert_sql, params)
        affected = cur.rowcount
        if affected != 1:
            conn.rollback()
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return {'error': f'Insert affected {affected} rows'}
        # Try fetch inserted row (best effort). Attempt predicate on first column if numeric/string
        try:
            predicate_val = first_value
            cur.execute(f"SELECT TOP 1 * FROM {table} WHERE [{first_col}] = ? ORDER BY [{first_col}] DESC", (predicate_val,))
            row = cur.fetchone()
            headers = [d[0] for d in cur.description]
            inserted = {h: (row[i] if row else None) for i,h in enumerate(headers)}
        except Exception:
            inserted = {}
        conn.commit()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return {'inserted': True, 'table': table, 'column': first_col, 'value': first_value, 'extras': extras, 'sql': insert_sql, 'row': inserted}
    except Exception as e:
        msg = str(e)
        if 'Conversion failed when converting date' in msg:
            try:
                if 'schema_rows' in locals():
                    schema_info = [{ 'column': r[0], 'nullable': r[1], 'default': r[2], 'type': r[3]} for r in schema_rows]
                else:
                    schema_info = []
            except Exception:
                schema_info = []
            return {'error': msg, 'schema': schema_info, 'hint': 'A date/datetime column requires an ISO value like 2025-10-08'}
        return {'error': msg}

def _attempt_data_preview(table_tokens: List[str], col_map: Dict[str, List[str]], app_config: AppConfiguration, limit_rows: int = 20) -> Tuple[str, str] | Tuple[None, None]:  # (formatted_text, executed_sql)
    if not table_tokens:
        return (None, None)
    # choose first resolvable token to fully qualified name
    fq_table = None
    raw_cols: List[str] = []
    for tok in table_tokens:
        # direct match
        if tok in col_map:
            fq_table = tok
            raw_cols = col_map.get(tok, [])
            break
        # suffix match schema.table
        for key in col_map.keys():
            if key.lower().endswith('.'+tok.split('.')[-1]):
                fq_table = key
                raw_cols = col_map.get(key, [])
                break
        if fq_table:
            break
    if not fq_table:
        # fallback: query INFORMATION_SCHEMA to find a table matching token (ignoring schema)
        try:
            conn_probe = _connect_db(app_config)
            curp = conn_probe.cursor()
            for tok in table_tokens:
                curp.execute("SELECT TOP 1 TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE LOWER(TABLE_NAME)=?", (tok.lower(),))
                row = curp.fetchone()
                if row:
                    fq_table = f"{row[0]}.{row[1]}"
                    # fetch columns
                    curp.execute("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", (row[0], row[1]))
                    raw_cols = [f"{r[0]}:{r[1]}" for r in curp.fetchall()]
                    break
            try:
                curp.close()
                conn_probe.close()
            except Exception:
                pass
        except Exception:
            fq_table = None
        if not fq_table:
            return (None, None)
    # derive columns (avoid *). If empty, attempt fuzzy column name fallback.
    cols = [c.split(':',1)[0] for c in raw_cols][:25]
    if not cols and fq_table:
        # Fallback: pull columns live then fuzzy pick a subset (first 10 alphabetically)
        try:
            conn_probe2 = _connect_db(app_config)
            cur2 = conn_probe2.cursor()
            schema_part, table_part = fq_table.split('.',1) if '.' in fq_table else ('dbo', fq_table)
            cur2.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", (schema_part, table_part))
            fetched = [r[0] for r in cur2.fetchall()]
            try:
                cur2.close()
                conn_probe2.close()
            except Exception:
                pass
            cols = fetched[:25]
        except Exception:
            pass
    if not cols:
        return (None, None)
    select_list = ', '.join(f'[{c}]' for c in cols)
    executed_sql = f"SELECT TOP {limit_rows} {select_list} FROM {fq_table};"
    # execute
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute(executed_sql)
        desc = [d[0] for d in cur.description]
        data_rows = cur.fetchall()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        preview_text = _format_preview(desc, data_rows, max_rows=limit_rows)
        return (preview_text, executed_sql)
    except Exception as e:  # pragma: no cover (env specific)
        return (f'(data preview failed: {e})', executed_sql)

UPDATE_REGEXES = [
    re.compile(r"update\s+([A-Za-z0-9_\.]+)\s+set\s+([A-Za-z0-9_]+)\s*=\s*([^\s]+)\s+where\s+([A-Za-z0-9_]+)\s*=\s*([^\s]+)", re.IGNORECASE),
    re.compile(r"change\s+([A-Za-z0-9_]+)\s+to\s+([^\s]+)\s+where\s+([A-Za-z0-9_]+)\s*=\s*([^\s]+)\s+in\s+([A-Za-z0-9_\.]+)", re.IGNORECASE),
    re.compile(r"set\s+([A-Za-z0-9_]+)\s+to\s+([^\s]+)\s+in\s+([A-Za-z0-9_\.]+)\s+where\s+([A-Za-z0-9_]+)\s*=\s*([^\s]+)", re.IGNORECASE),
]

CONTEXTUAL_CHANGE_REGEX = re.compile(
    r"(?:change|update)\s+"
    r"(?:the\s+value\s+|this\s+(?:value|email|item|record)\s+)?"
    r"(?:in\s+this\s+table\s+)?"  # optional filler
    r"(?:from\s+)?"  # allow 'from' keyword optionally
    r"['\"]?([A-Za-z0-9_ @\.\-]+?)['\"]?\s+to\s+['\"]?([A-Za-z0-9_ @\.\-]+)['\"]?",
    re.IGNORECASE,
)

def _attempt_contextual_change(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    m = CONTEXTUAL_CHANGE_REGEX.search(text)
    if not m:
        return None
    old_val_raw, new_val_raw = m.groups()
    old_val = old_val_raw.strip()
    new_val = new_val_raw.strip()
    # Guard: if normalization accidentally mapped part of old_val to intent keyword, reject
    if old_val in INTENT_SYNONYMS.get('insert', set()) or old_val in INTENT_SYNONYMS.get('update', set()):
        return {'error': f"Ambiguous source value '{old_val_raw}' interpreted as command keyword; please specify column."}
    ctx = LAST_PREVIEWS.get(session_id) or LAST_PREVIEWS.get(GLOBAL_LAST_PREVIEW_KEY)
    if not ctx:
        return {'error': 'No previous table context for change operation'}
    table = ctx.get('table')
    cols = ctx.get('columns') or []
    if not table or not cols:
        return {'error': 'Incomplete preview context'}
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        candidate_hits = []
        for c in cols[:50]:
            if old_val and not old_val.isdigit() and re.search(r"id$", c, re.IGNORECASE):
                continue
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE [{c}] = ?", (old_val,))
                cnt = cur.fetchone()[0]
                if cnt:
                    candidate_hits.append((c, cnt))
            except Exception:
                continue
        singles = [(c, cnt) for c, cnt in candidate_hits if cnt == 1]
        if len(singles) == 1:
            target_col = singles[0][0]
            try:
                # snapshot BEFORE
                cur.execute(f"SELECT TOP 2 * FROM {table} WHERE [{target_col}] = ?", (old_val,))
                before_rows = cur.fetchall()
                if len(before_rows) != 1:
                    return {'error': f'Ambiguous update (matched {len(before_rows)} rows before change)'}
                headers_before = [d[0] for d in cur.description]
                before_snapshot = {h: before_rows[0][i] for i,h in enumerate(headers_before)}
                cur.execute(f"UPDATE {table} SET [{target_col}] = ? WHERE [{target_col}] = ?", (new_val, old_val))
                affected = cur.rowcount
                if affected == 1:
                    cur.execute(f"SELECT TOP 1 * FROM {table} WHERE [{target_col}] = ?", (new_val,))
                    row = cur.fetchone()
                    headers = [d[0] for d in cur.description]
                    conn.commit()
                    # record history
                    _record_update(session_id, {
                        'table': table,
                        'key_column': target_col,
                        'key_value_before': before_snapshot.get(target_col),
                        'key_value_after': new_val,
                        'rows_before': [before_snapshot],
                        'columns_changed': [target_col]
                    })
                    try:
                        cur.close()
                        conn.close()
                    except Exception:
                        pass
                    return {'updated': True, 'table': table, 'column': target_col, 'old_value': old_val, 'new_value': new_val, 'affected': affected, 'sql': f"UPDATE {table} SET [{target_col}] = ? WHERE [{target_col}] = ?", 'row': {h: (row[i] if row else None) for i,h in enumerate(headers)}}
                conn.rollback()
                try:
                    cur.close()
                    conn.close()
                except Exception:
                    pass
                return {'error': f'Ambiguous update (affected {affected} rows)'}
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return {'error': f'Execution failed: {e}'}
        if not candidate_hits:
            return {'error': f"Value '{old_val}' not found uniquely in preview columns"}
        multi_info = ', '.join(f"{c}({cnt})" for c, cnt in candidate_hits)
        if len(singles) > 1:
            return {'error': 'Multiple single-row columns matched value; specify column explicitly'}
        return {'error': f'Ambiguous value (matches: {multi_info}). Provide column and key.'}
    except Exception as e:
        return {'error': str(e)}

def _strip_quotes(v: str) -> str:
    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
        return v[1:-1]
    return v

def _parse_update_intent(text: str) -> Dict[str, Any] | None:
    t = text.strip()
    if not t:
        return None
    lower = t.lower()
    if not any(k in lower for k in ('update','change','set ')):
        return None
    # normalize whitespace
    for rx in UPDATE_REGEXES:
        m = rx.search(t)
        if m:
            # pattern dependent extraction
            if rx.pattern.startswith('update'):
                table, col, new_val, key_col, key_val = m.groups()
            elif rx.pattern.startswith('change'):
                # change <col> to <val> where <key>=<val> in <table>
                col, new_val, key_col, key_val, table = m.groups()
            else:  # set <col> to <val> in <table> where <key>=<val>
                col, new_val, table, key_col, key_val = m.groups()
            return {
                'table_raw': table,
                'column': col,
                'new_value_raw': new_val,
                'key_column': key_col,
                'key_value_raw': key_val
            }
    return None

MULTI_UPDATE_REGEX = re.compile(r"update\s+([A-Za-z0-9_\.]+)\s+set\s+(.+?)\s+where\s+([A-Za-z0-9_]+)\s*=\s*([^\s]+)", re.IGNORECASE)

def _parse_multi_update(text: str) -> Dict[str, Any] | None:
    m = MULTI_UPDATE_REGEX.search(text)
    if not m:
        return None
    table, assigns_raw, key_col, key_val_raw = m.groups()
    # split assignments by comma or ' and '
    parts = re.split(r",| and ", assigns_raw)
    assignments = []
    for p in parts:
        if not p.strip():
            continue
        m2 = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*([^\s]+)\s*", p.strip())
        if m2:
            col, val = m2.groups()
            assignments.append((col, val))
    if len(assignments) <= 1:
        return None  # let single-column path handle
    return {
        'table_raw': table,
        'assignments_raw': assignments,
        'key_column': key_col,
        'key_value_raw': key_val_raw
    }

def _record_update(session_id: str, op: Dict[str, Any]):
        """Push an update operation onto history for potential revert.

        Stores at most 50 recent operations per session. Each op should contain:
            table: str
            key_column: str
            key_value_before: any
            key_value_after: any (optional; only needed if key itself changed)
            rows_before: list[dict]
            columns_changed: list[str]
        """
        stack = UPDATE_HISTORY.setdefault(session_id, [])
        stack.append(op)
        if len(stack) > 50:  # trim oldest
                del stack[0:len(stack)-50]

def _attempt_revert(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    """Attempt revert based on user text.

    Supports phrases like:
      revert
      undo
      rollback
      revert last 3
      undo 2
    """
    lower = text.lower().strip()
    if not any(k in lower for k in ('revert','undo','rollback')):
        return None
    m_multi = re.search(r"(?:revert|undo|rollback)\s+last\s+(\d+)", lower)
    if not m_multi:
        m_multi = re.search(r"(?:revert|undo|rollback)\s+(\d+)", lower)
    n = 1
    if m_multi:
        try:
            n = max(1, int(m_multi.group(1)))
        except Exception:
            n = 1
    stack = UPDATE_HISTORY.get(session_id) or []
    if not stack:
        return {'error': 'No update to revert'}
    n = min(n, len(stack))
    # copy last n without mutating until success
    ops: List[Dict[str, Any]] = list(reversed(stack[-n:]))  # newest first
    # Apply in reverse order of execution (already LIFO) -> revert sequentially newest first
    total_rows = 0
    tables = set()
    cols_all: set[str] = set()
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        for op in ops:
            table = op['table']
            key_col = op['key_column']
            cols_changed = op['columns_changed']
            rows_before = op['rows_before']
            key_changed = key_col in cols_changed
            for rb in rows_before:
                # If key was changed we need the AFTER value (stored) to locate the row
                where_val = op.get('key_value_after') if key_changed else rb.get(key_col)
                set_clause = ', '.join(f"[{c}] = ?" for c in cols_changed)
                params = [rb[c] for c in cols_changed] + [where_val]
                cur.execute(f"UPDATE {table} SET {set_clause} WHERE [{key_col}] = ?", params)
                total_rows += max(cur.rowcount, 0)
            tables.add(table)
            cols_all.update(cols_changed)
        conn.commit()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        # success: now truncate consumed ops
        del stack[-n:]
        UPDATE_HISTORY[session_id] = stack
        return {'reverted': True, 'operations': n, 'rows': total_rows, 'tables': list(tables), 'columns': sorted(cols_all)}
    except Exception as e:
        return {'error': f'revert_failed: {e}'}

# --- Mass update / confirmation / history listing / soft delete helpers ---
PENDING_UPDATES: Dict[str, Dict[str, Any]] = {}
GENERIC_MASS_UPDATE_REGEX = re.compile(r"update\s+([A-Za-z0-9_\.]+)\s+set\s+(.+?)\s+where\s+(.+)$", re.IGNORECASE)
CONFIRM_UPDATE_REGEX = re.compile(r"(confirm|apply)\s+update(?:\s+([A-Za-z0-9_-]+))?", re.IGNORECASE)
SHOW_HISTORY_REGEX = re.compile(r"show\s+(?:last\s+)?changes(?:\s+(\d+))?", re.IGNORECASE)
SOFT_DELETE_REGEX = re.compile(r"soft\s+delete(?:\s+from)?\s+([A-Za-z0-9_\.]+)?\s*(?:where\s+(.+))?", re.IGNORECASE)
RESTORE_REGEX = re.compile(r"restore(?:\s+from)?\s+([A-Za-z0-9_\.]+)?\s*(?:where\s+(.+))?", re.IGNORECASE)
FORBIDDEN_CLAUSE = re.compile(r";|--|/\*|drop\s|alter\s", re.IGNORECASE)

def _sanitize_clause(clause: str) -> bool:
    if not clause:
        return False
    if FORBIDDEN_CLAUSE.search(clause):
        return False
    return True

def _heuristic_key_column(cols: List[str]) -> str | None:
    for c in cols:
        if re.search(r"(^id$|_id$)", c, re.IGNORECASE):
            return c
    return cols[0] if cols else None

def _attempt_show_history(session_id: str, text: str) -> Dict[str, Any] | None:
    m = SHOW_HISTORY_REGEX.search(text)
    if not m:
        return None
    limit = 10
    if m.group(1):
        try:
            limit = max(1, min(50, int(m.group(1))))
        except Exception:
            pass
    hist = list(reversed(UPDATE_HISTORY.get(session_id, [])))[:limit]
    summary = []
    for i, op in enumerate(hist, 1):
        summary.append({
            'idx': i,
            'table': op.get('table'),
            'columns': op.get('columns_changed'),
            'rows': len(op.get('rows_before') or []),
            'mass': op.get('mass', False),
            'ts': op.get('ts')
        })
    return {'history': summary}

def _attempt_mass_update(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    m = GENERIC_MASS_UPDATE_REGEX.search(text)
    if not m:
        return None
    table_raw, set_clause, where_clause = m.groups()
    if not (_sanitize_clause(set_clause) and _sanitize_clause(where_clause)):
        return {'error': 'Unsafe clause detected'}
    table_fq = table_raw if '.' in table_raw else f"dbo.{table_raw}"
    assigns = []
    for part in re.split(r",", set_clause):
        p = part.strip()
        m2 = re.match(r"([A-Za-z0-9_]+)\s*=", p)
        if m2:
            assigns.append(m2.group(1))
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table_fq} WHERE {where_clause}")
        count = cur.fetchone()[0]
        if count == 0:
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return {'error': 'No rows match condition'}
        cur.execute(f"SELECT TOP 5 * FROM {table_fq} WHERE {where_clause}")
        rows = cur.fetchall()
        headers = [d[0] for d in cur.description]
        key_col = _heuristic_key_column(headers) or headers[0]
        cur.execute(f"SELECT * FROM {table_fq} WHERE {where_clause}")
        all_rows = cur.fetchall()
        before_rows = []
        for r in all_rows[:200]:
            before_rows.append({h: r[i] for i, h in enumerate(headers)})
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        op_id = uuid.uuid4().hex[:8]
        pendings = PENDING_UPDATES.setdefault(session_id, {})
        pendings[op_id] = {
            'op_id': op_id,
            'table': table_fq,
            'set_clause': set_clause,
            'where_clause': where_clause,
            'columns_changed': assigns,
            'rows_before': before_rows,
            'row_count': count,
            'key_column': key_col,
            'sql': f"UPDATE {table_fq} SET {set_clause} WHERE {where_clause}",
        }
        preview_list = []
        for r in rows:
            preview_list.append({h: r[i] for i, h in enumerate(headers)})
        return {
            'pending_mass_update': True,
            'op_id': op_id,
            'table': table_fq,
            'rows': count,
            'columns_changed': assigns,
            'preview': preview_list,
            'confirm': f"confirm update {op_id}" if len(pendings) == 1 else f"confirm update {op_id}"
        }
    except Exception as e:
        return {'error': f'Preparation failed: {e}'}

def _attempt_confirm_update(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    m = CONFIRM_UPDATE_REGEX.search(text)
    if not m:
        return None
    op_id = m.group(2)
    pendings = PENDING_UPDATES.get(session_id) or {}
    if not pendings:
        return {'error': 'No pending update to confirm'}
    if not op_id:
        op = list(pendings.values())[-1]
    else:
        op = pendings.get(op_id)
        if not op:
            return {'error': f'Pending update id {op_id} not found'}
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        before_rows = op['rows_before']
        cur.execute(op['sql'])
        affected = cur.rowcount
        conn.commit()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        _record_update(session_id, {
            'table': op['table'],
            'key_column': op['key_column'],
            'key_value_before': None,
            'key_value_after': None,
            'rows_before': before_rows,
            'columns_changed': op['columns_changed'],
            'mass': True
        })
        try:
            del pendings[op['op_id']]
        except Exception:
            pass
        return {'updated': True, 'mass': True, 'table': op['table'], 'rows': affected, 'columns': op['columns_changed']}
    except Exception as e:
        return {'error': f'Execution failed: {e}'}

def _attempt_soft_delete(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    m = SOFT_DELETE_REGEX.search(text)
    if not m:
        return None
    table_raw, where_clause = m.groups()
    ctx = LAST_PREVIEWS.get(session_id) or LAST_PREVIEWS.get(GLOBAL_LAST_PREVIEW_KEY)
    if not table_raw and ctx:
        table_raw = ctx.get('table')
    if not table_raw:
        return {'error': 'Table not specified and no preview context'}
    table_fq = table_raw if '.' in table_raw else f"dbo.{table_raw}"
    where_clause = where_clause or '1=1'
    if not _sanitize_clause(where_clause):
        return {'error': 'Unsafe where clause'}
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute(f"SELECT TOP 0 * FROM {table_fq}")
        headers = [d[0] for d in cur.description]
        has_is_deleted = any(h.lower() == 'is_deleted' for h in headers)
        has_deleted_at = any(h.lower() == 'deleted_at' for h in headers)
        if not (has_is_deleted or has_deleted_at):
            return {'error': 'No soft delete columns (is_deleted or deleted_at) found'}
        sets = []
        if has_is_deleted:
            sets.append('is_deleted = 1')
        if has_deleted_at:
            sets.append('deleted_at = GETUTCDATE()')
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return _attempt_mass_update(session_id, f"update {table_fq} set {', '.join(sets)} where {where_clause}", app_config)
    except Exception as e:
        return {'error': f'Soft delete failed: {e}'}

def _attempt_restore(session_id: str, text: str, app_config: AppConfiguration) -> Dict[str, Any] | None:
    m = RESTORE_REGEX.search(text)
    if not m:
        return None
    table_raw, where_clause = m.groups()
    ctx = LAST_PREVIEWS.get(session_id) or LAST_PREVIEWS.get(GLOBAL_LAST_PREVIEW_KEY)
    if not table_raw and ctx:
        table_raw = ctx.get('table')
    if not table_raw:
        return {'error': 'Table not specified and no preview context'}
    table_fq = table_raw if '.' in table_raw else f"dbo.{table_raw}"
    where_clause = where_clause or '1=1'
    if not _sanitize_clause(where_clause):
        return {'error': 'Unsafe where clause'}
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute(f"SELECT TOP 0 * FROM {table_fq}")
        headers = [d[0] for d in cur.description]
        has_is_deleted = any(h.lower() == 'is_deleted' for h in headers)
        has_deleted_at = any(h.lower() == 'deleted_at' for h in headers)
        if not (has_is_deleted or has_deleted_at):
            return {'error': 'No soft delete columns to restore'}
        sets = []
        if has_is_deleted:
            sets.append('is_deleted = 0')
        if has_deleted_at:
            sets.append('deleted_at = NULL')
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return _attempt_mass_update(session_id, f"update {table_fq} set {', '.join(sets)} where {where_clause}", app_config)
    except Exception as e:
        return {'error': f'Restore failed: {e}'}

def _execute_multi_update(session_id: str, intent: Dict[str, Any], app_config: AppConfiguration) -> Dict[str, Any]:
    table_raw = intent['table_raw']
    if '.' not in table_raw:
        col_map = CATALOG_STATE.get('columns_by_table') or {}
        resolved = None
        for k in col_map.keys():
            if k.lower().endswith('.'+table_raw.lower()):
                resolved = k
                break
        table_fq = resolved or f"dbo.{table_raw}"
    else:
        table_fq = table_raw
    key_col = intent['key_column']
    key_val_raw = intent['key_value_raw']
    assignments_raw = intent['assignments_raw']  # list[(col,val)]
    def infer(v: str):
        v = v.strip()
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        if re.fullmatch(r"-?\d+", v):
            return int(v), 'int'
        if re.fullmatch(r"-?\d+\.\d+", v):
            try:
                return float(v), 'float'
            except Exception:
                return v, 'str'
        if v.lower() in ('null','none'):
            return None, 'null'
        return v, 'str'
    key_val = infer(key_val_raw)
    assigns = [(col, infer(val)) for col,val in assignments_raw]
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        # fetch before row(s)
        cur.execute(f"SELECT TOP 2 * FROM {table_fq} WHERE [{key_col}] = ?", (key_val,))
        rows = cur.fetchall()
        if len(rows) != 1:
            return {'error': f'Unsafe update (matched {len(rows)} rows); specify a unique key value'}
        headers = [d[0] for d in cur.description]
        before = {h: rows[0][i] for i,h in enumerate(headers)}
        # verify columns exist
        cols_present = {h.lower() for h in headers}
        for c,_ in assigns:
            if c.lower() not in cols_present:
                return {'error': f'Column not found: {c}'}
        set_clause = ', '.join(f"[{c}] = ?" for c,_ in assigns)
        params = [v for _,v in assigns] + [key_val]
        cur.execute(f"UPDATE {table_fq} SET {set_clause} WHERE [{key_col}] = ?", params)
        if cur.rowcount != 1:
            conn.rollback()
            return {'error': f'Update affected {cur.rowcount} rows; aborted'}
        # fetch after
        cur.execute(f"SELECT TOP 1 * FROM {table_fq} WHERE [{key_col}] = ?", (key_val,))
        row_after = cur.fetchone()
        after = {h: row_after[i] for i,h in enumerate(headers)}
        conn.commit()
        # record history for revert
        _record_update(session_id, {
            'table': table_fq,
            'key_column': key_col,
            'key_value': key_val,
            'rows_before': [before],
            'columns_changed': [c for c,_ in assigns]
        })
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        return {'updated': True, 'table': table_fq, 'columns': [c for c,_ in assigns], 'key_column': key_col, 'key_value': key_val, 'sql': f"UPDATE {table_fq} SET {set_clause} WHERE [{key_col}] = ?", 'before': before, 'after': after}
    except Exception as e:
        return {'error': str(e)}

def _execute_safe_update(session_id: str, intent: Dict[str, Any], app_config: AppConfiguration) -> Dict[str, Any]:
    table_raw = intent['table_raw']
    # Qualify schema if missing
    if '.' not in table_raw:
        # Attempt to locate table using existing column map
        col_map = CATALOG_STATE.get('columns_by_table') or {}
        table_lower = table_raw.lower()
        resolved = None
        for k in col_map.keys():
            if k.lower().endswith('.'+table_lower):
                resolved = k
                break
        table_fq = resolved or f"dbo.{table_raw}"
    else:
        table_fq = table_raw
    col = intent['column']
    key_col = intent['key_column']
    new_val_raw = _strip_quotes(intent['new_value_raw'])
    key_val_raw = _strip_quotes(intent['key_value_raw'])
    # primitive type inference
    def infer(v: str):
        if re.fullmatch(r"-?\d+", v):
            return int(v), 'int'
        if re.fullmatch(r"-?\d+\.\d+", v):
            try:
                return float(v), 'float'
            except Exception:
                return v, 'str'
        if v.lower() in ('null','none'):
            return None, 'null'
        return v, 'str'
    new_val, new_kind = infer(new_val_raw)
    key_val, key_kind = infer(key_val_raw)
    executed = {
        'table': table_fq,
        'column': col,
        'key_column': key_col,
        'new_value': new_val,
        'key_value': key_val,
        'value_types': {'new': new_kind, 'key': key_kind}
    }
    # Safety: forbid mass update by requiring key value not None
    if key_val is None:
        executed['error'] = 'Refusing to update: key value is NULL'
        return executed
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        # fetch BEFORE snapshot for revert safety
        try:
            cur.execute(f"SELECT TOP 2 * FROM {table_fq} WHERE [{key_col}] = ?", (key_val,))
            before_rows = cur.fetchall()
            if len(before_rows) != 1:
                executed['error'] = f'Unsafe update (matched {len(before_rows)} rows before change)'
                try:
                    cur.close()
                    conn.close()
                except Exception:  # pragma: no cover
                    pass
                return executed
            headers_before = [d[0] for d in cur.description]
            before_snapshot = {h: before_rows[0][i] for i,h in enumerate(headers_before)}
        except Exception as fe:
            executed['error'] = f'Failed to snapshot row: {fe}'
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return executed
        # Verify table & columns existence (best effort)
        try:
            cur.execute("SELECT TOP 1 * FROM "+table_fq+" WHERE 1=0")
            # columns description available
            cols_present = {d[0].lower() for d in cur.description}
            if col.lower() not in cols_present or key_col.lower() not in cols_present:
                executed['error'] = 'Column not found'
                try:
                    cur.close()
                    conn.close()
                except Exception:
                    pass
                return executed
        except Exception as ve:
            executed['error'] = f'table verification failed: {ve}'
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return executed
        # Perform update with parameterization
        update_sql = f"UPDATE {table_fq} SET [{col}] = ? WHERE [{key_col}] = ?"
        cur.execute(update_sql, (new_val, key_val))
        affected = cur.rowcount
        if affected != 1:
            conn.rollback()
            executed['error'] = f'Unsafe update (affected={affected}); aborted'
            try:
                cur.close()
                conn.close()
            except Exception:
                pass
            return executed
        # fetch updated row
        select_sql = f"SELECT TOP 1 * FROM {table_fq} WHERE [{key_col}] = ?"
        cur.execute(select_sql, (key_val,))
        row = cur.fetchone()
        headers = [d[0] for d in cur.description]
        conn.commit()
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
        executed['updated'] = True
        executed['affected'] = affected
        executed['row'] = {h: (row[i] if row else None) for i,h in enumerate(headers)}
        executed['sql'] = update_sql
        executed['select_sql'] = select_sql
        # Record history (skip if updating key column itself to avoid complex revert logic unless unaffected)
        if col == key_col:
            executed['history_note'] = 'Key column changed; revert may not be supported.'
            # Still attempt to record using after value for lookup
            _record_update(session_id, {
                'table': table_fq,
                'key_column': key_col,
                'key_value_before': before_snapshot.get(key_col),
                'key_value_after': executed['row'].get(key_col),
                'rows_before': [before_snapshot],
                'columns_changed': [col]
            })
        else:
            _record_update(session_id, {
                'table': table_fq,
                'key_column': key_col,
                'key_value_before': before_snapshot.get(key_col),
                'key_value_after': before_snapshot.get(key_col),
                'rows_before': [before_snapshot],
                'columns_changed': [col]
            })
        return executed
    except Exception as e:
        executed['error'] = str(e)
        return executed


def _extract_metadata(cur) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:  # tables, columns, rels
    tables_sql = """SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"""
    cur.execute(tables_sql)
    tables = [{"schema": r[0], "table": r[1]} for r in cur.fetchall()]
    cols_sql = """SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS"""
    cur.execute(cols_sql)
    columns = [{"schema": r[0], "table": r[1], "column": r[2], "type": r[3], "nullable": r[4]} for r in cur.fetchall()]
    rels_sql = """
    SELECT fk.name, sch1.name AS fk_schema, t1.name AS fk_table, c1.name AS fk_column,
           sch2.name AS pk_schema, t2.name AS pk_table, c2.name AS pk_column
    FROM sys.foreign_keys fk
    INNER JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
    INNER JOIN sys.tables t1 ON fkc.parent_object_id = t1.object_id
    INNER JOIN sys.schemas sch1 ON t1.schema_id = sch1.schema_id
    INNER JOIN sys.columns c1 ON fkc.parent_object_id = c1.object_id AND fkc.parent_column_id = c1.column_id
    INNER JOIN sys.tables t2 ON fkc.referenced_object_id = t2.object_id
    INNER JOIN sys.schemas sch2 ON t2.schema_id = sch2.schema_id
    INNER JOIN sys.columns c2 ON fkc.referenced_object_id = c2.object_id AND fkc.referenced_column_id = c2.column_id
    """
    try:
        cur.execute(rels_sql)
        rels = [{
            "fk_name": r[0],
            "fk_schema": r[1], "fk_table": r[2], "fk_column": r[3],
            "pk_schema": r[4], "pk_table": r[5], "pk_column": r[6]
        } for r in cur.fetchall()]
    except Exception:
        rels = []
    return tables, columns, rels


def _build_docs(tables: List[Dict[str, Any]], columns: List[Dict[str, Any]], rels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for t in tables:
        related = [r for r in rels if r['fk_table'] == t['table'] or r['pk_table'] == t['table']]
        rel_summary = "; ".join({f"{r['fk_table']}.{r['fk_column']}->{r['pk_table']}.{r['pk_column']}" for r in related})
        cols = [c for c in columns if c['table'] == t['table'] and c['schema'] == t['schema']]
        col_list = ", ".join(f"{c['column']}({c['type']})" for c in cols[:40])
        content = f"TABLE {t['schema']}.{t['table']} cols: {col_list}. rels: {rel_summary}"[:1500]
        docs.append({
            "id": f"table::{t['schema']}.{t['table']}",
            "kind": "table",
            "schema": t['schema'],
            "table": t['table'],
            "column": "",
            "content": content,
        })
    for c in columns:
        content = f"COLUMN {c['schema']}.{c['table']}.{c['column']} type={c['type']} nullable={c['nullable']}"[:1500]
        docs.append({
            "id": f"col::{c['schema']}.{c['table']}.{c['column']}",
            "kind": "column",
            "schema": c['schema'],
            "table": c['table'],
            "column": c['column'],
            "content": content,
        })
    for r in rels:
        content = f"REL {r['fk_schema']}.{r['fk_table']}.{r['fk_column']} -> {r['pk_schema']}.{r['pk_table']}.{r['pk_column']}"[:1500]
        docs.append({
            "id": f"rel::{r['fk_name']}",
            "kind": "relationship",
            "schema": r['fk_schema'],
            "table": r['fk_table'],
            "column": r['fk_column'],
            "content": content,
        })
    return docs


def _extract_table_tokens(text: str) -> List[str]:
    """Heuristic extraction of potential table identifiers from user text.

    Lowercases tokens and returns unique subset (limited to 25) to keep prompt compact.
    """
    if not text:
        return []
    candidates: set[str] = set()
    for tok in re.findall(r"[A-Za-z0-9_\.]{3,}", text):
        if tok.isdigit():
            continue
        norm = tok.strip('.').lower()
        if len(norm) < 3:
            continue
        candidates.add(norm)
        if len(candidates) >= 25:
            break
    return list(candidates)

def _fuzzy_catalog_match(token: str, choices: List[str], max_distance: int = 2) -> str | None:
    """Return best fuzzy match for token among choices within max_distance (Levenshtein)."""
    token_l = token.lower()
    best: tuple[int, str] | None = None
    for ch in choices:
        cl = ch.lower()
        # quick exact or suffix match
        if cl == token_l or cl.endswith('.'+token_l):
            return ch
        # length pre-filter
        if abs(len(cl) - len(token_l)) > max_distance:
            continue
        # compute distance (reuse _edit_distance logic but safe if not yet defined in file ordering)
        try:
            dist = _edit_distance(token_l, cl)
        except Exception:
            continue
        if dist <= max_distance:
            if not best or dist < best[0] or (dist == best[0] and len(ch) < len(best[1])):
                best = (dist, ch)
    return best[1] if best else None

def _fuzzy_resolve_tables(raw_tokens: List[str]) -> List[str]:
    """Map user tokens to catalog tables using fuzzy logic; preserves order of appearance.

    Falls back to original token if no match; duplicates removed while keeping first occurrence.
    """
    col_map = CATALOG_STATE.get('columns_by_table') or {}
    catalog_tables = list(col_map.keys())
    base_names = [t.split('.')[-1] for t in catalog_tables]
    resolved: List[str] = []
    seen: set[str] = set()
    misses: Dict[str, List[str]] = {}
    for tok in raw_tokens:
        match = _fuzzy_catalog_match(tok, catalog_tables, max_distance=2)
        if match:
            if match not in seen:
                resolved.append(match)
                seen.add(match)
        else:
            # gather up to 3 suggestions based on distance to base names
            cand: List[tuple[int,str]] = []
            for bn, full in zip(base_names, catalog_tables):
                try:
                    dist = _edit_distance(tok.lower(), bn.lower())
                except Exception:
                    continue
                if dist <= 2:
                    cand.append((dist, full))
            cand.sort(key=lambda x: (x[0], len(x[1])))
            if cand:
                misses[tok] = [c[1] for c in cand[:3]]
            else:
                misses[tok] = []
    if misses:
        CATALOG_STATE['last_fuzzy_misses'] = misses
    else:
        CATALOG_STATE.pop('last_fuzzy_misses', None)
    return resolved[:25]


def _suggest_joins(guessed_tables: List[str]) -> List[str]:
    """Suggest join relationships based on catalog stored foreign key metadata.

    Returns short textual join hints limited to 12 entries.
    """
    rels: List[Dict[str, Any]] = CATALOG_STATE.get('rels', []) or []
    if not rels or len(guessed_tables) < 2:
        return []
    guessed_core = {g.split('.')[-1] for g in guessed_tables}
    hints: List[str] = []
    for r in rels:
        fk_t = r.get('fk_table')
        pk_t = r.get('pk_table')
        if fk_t in guessed_core and pk_t in guessed_core:
            hints.append(f"{r.get('fk_schema')}.{fk_t}.{r.get('fk_column')} -> {r.get('pk_schema')}.{pk_t}.{r.get('pk_column')}")
            if len(hints) >= 12:
                break
    return hints


def _maybe_stub_catalog(app_config: AppConfiguration):
    """If required search/embedding configuration is missing, allow a development stub so UI can function.

    This sets the catalog state to 'ready' with zero tables if we cannot actually build the index.
    Only activates when current status is idle or error.
    """
    if CATALOG_STATE.get("status") in ("idle", "error"):
        if not (app_config.azure_search_endpoint and app_config.azure_search_api_key and app_config.azure_search_index):
            # Provide stub ready state so chat endpoints don't block local dev.
            CATALOG_STATE.update({
                "status": "ready",
                "tables": 0,
                "columns": 0,
                "relationships": 0,
                "documents": 0,
                "stub": True,
            })
            logger.info("Catalog stub activated (search config missing).")

def _ensure_min_catalog(app_config: AppConfiguration):  # lightweight direct DB snapshot if catalog not built
    if CATALOG_STATE.get('columns_by_table') and CATALOG_STATE.get('table_names'):
        return
    # Attempt direct DB introspection
    try:
        conn = _connect_db(app_config)
        cur = conn.cursor()
        cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'")
        tables = [(r[0], r[1]) for r in cur.fetchall()][:500]
        col_map: Dict[str, List[str]] = {}
        for sch, tbl in tables:
            key = f"{sch}.{tbl}"
            try:
                cur.execute("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION", (sch, tbl))
                cols = [f"{r[0]}:{r[1]}" for r in cur.fetchall()[:150]]
                col_map[key] = cols
            except Exception:
                continue
        CATALOG_STATE.setdefault('status', 'ready')
        CATALOG_STATE['table_names'] = list(col_map.keys())
        CATALOG_STATE['columns_by_table'] = col_map
        CATALOG_STATE.setdefault('rels', [])
    except Exception as e:
        logger.warning("_ensure_min_catalog failed: %s", e)
    finally:
        try:
            cur.close()  # type: ignore
            conn.close()  # type: ignore
        except Exception:
            pass


# @router.post("/catalog/rebuild")
# async def rebuild_catalog(app_config: AppConfiguration = Depends(get_app_config)):
#     pass

@router.get("/catalog/status")
async def catalog_status():
    return CATALOG_STATE


@router.get("/catalog/table/{table_name}")
async def catalog_table_detail(table_name: str):
    """Return columns and relationships for a given table name (case-insensitive).

    Accepts either fully qualified schema.table or just table. If multiple schema matches
    exist for unqualified name, returns all.
    """
    if CATALOG_STATE.get('status') != 'ready':
        raise HTTPException(status_code=409, detail="Catalog not ready")
    col_map = CATALOG_STATE.get('columns_by_table') or {}
    rels = CATALOG_STATE.get('rels') or []
    name_l = table_name.lower()
    matches = []
    for full in col_map.keys():
        if full.lower() == name_l or full.lower().endswith('.'+name_l):
            matches.append(full)
    if not matches:
        raise HTTPException(status_code=404, detail="Table not found in catalog")
    result = []
    for m in matches:
        schema, tbl = m.split('.',1)
        cols = col_map.get(m, [])
        rel_hits = [r for r in rels if r['fk_table']==tbl or r['pk_table']==tbl]
        result.append({
            "table": m,
            "columns": cols,
            "relationships": rel_hits[:50]
        })
    return {"results": result}


# @router.post("/catalog/force-ready")
# async def catalog_force_ready(app_config: AppConfiguration = Depends(get_app_config)):
#     pass

def _build_prompt(question: str, contexts: List[Dict[str, Any]]) -> str:
    ctx_text = "\n".join(f"[{c['kind']}] {c['content']}" for c in contexts[:8])
    return (
        "You are a SQL assistant. Use only provided context. Return JSON with keys answer, sql, confidence.\n"\
        f"Context:\n{ctx_text}\nQuestion: {question}\nRespond strictly in JSON."  # minimal prompt
    )


def _build_prompt_with_history(question: str, contexts: List[Dict[str, Any]], history: List[Dict[str, Any]]) -> str:
    """Build richer prompt including recent conversation (follow-up chat support)."""
    recent = history[-8:]
    hist_lines: List[str] = []
    for m in recent:
        role = m.get('role','')
        content = (m.get('content') or '').strip()
        if m.get('sql'):
            sql_snip = m['sql'][:300] + ('...' if len(m['sql'])>300 else '')
            content += f"\n(SQL: {sql_snip})"
        content = content[:600] + ('...' if len(content)>600 else '')
        hist_lines.append(f"{role}: {content}")
    hist_text = "\n".join(hist_lines)
    ctx_text = "\n".join(f"[{c['kind']}] {c['content']}" for c in contexts[:8])
    return (
        "You are a SQL assistant. Use only provided context and conversation history. "
        "Return JSON: {\"answer\":string, \"sql\":string or empty if not sure, \"confidence\":0-1}.\n"
        f"History:\n{hist_text}\n---\nContext:\n{ctx_text}\nQuestion: {question}\nRespond strictly in JSON."
    )


def _load_session(session_id: str) -> List[Dict[str, Any]]:
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_session(session_id: str, messages: List[Dict[str, Any]]):
    try:
        path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("Failed to persist session %s", session_id)


@router.get("/sessions")
async def list_sessions() -> Dict[str, Any]:
    out = []
    for fname in os.listdir(SESSIONS_DIR):
        if fname.endswith('.json'):
            sid = fname[:-5]
            try:
                mtime = os.path.getmtime(os.path.join(SESSIONS_DIR, fname))
            except Exception:
                mtime = 0
            out.append({"session_id": sid, "modified": mtime})
    out.sort(key=lambda x: x['modified'], reverse=True)
    return {"sessions": out[:200]}

@router.get("/session/{session_id}")
async def get_session(session_id: str) -> Dict[str, Any]:
    msgs = _load_session(session_id)
    return {"session_id": session_id, "messages": msgs}


# @router.post("/chat")
# async def chat(message: Dict[str, Any] = Body(...), app_config: AppConfiguration = Depends(get_app_config)):
#     pass


# @router.post("/chat/stream")
# async def chat_stream(message: Dict[str, Any] = Body(...), app_config: AppConfiguration = Depends(get_app_config)):
#     pass


# @router.post("/chat/apply-view")
# async def apply_view(payload: Dict[str, Any] = Body(...), app_config: AppConfiguration = Depends(get_app_config)):
#     pass


@router.get("/debug/session/{session_id}")
async def debug_session(session_id: str):
    messages = CHAT_SESSIONS.get(session_id) or _load_session(session_id)
    return {"session_id": session_id, "count": len(messages), "messages": messages[-20:]}

from pydantic import BaseModel
import sqlite3
from typing import Optional, Dict, Any

class SQLCrudRequest(BaseModel):
    db_path: str
    operation: str  # "create", "read", "update", "delete"
    table: str
    data: Optional[Dict[str, Any]] = None
    where: Optional[str] = None

@router.post("/sql/crud")
async def sql_crud(req: SQLCrudRequest):
    try:
        conn = sqlite3.connect(req.db_path)
        cur = conn.cursor()
        if req.operation == "create":
            if not req.data:
                raise HTTPException(status_code=400, detail="Missing data for create operation")
            keys = ','.join(req.data.keys())
            qmarks = ','.join(['?'] * len(req.data))
            values = list(req.data.values())
            cur.execute(f"INSERT INTO {req.table} ({keys}) VALUES ({qmarks})", values)
            conn.commit()
            return {"status": "success", "operation": "create"}
        elif req.operation == "read":
            where_clause = f"WHERE {req.where}" if req.where else ""
            cur.execute(f"SELECT * FROM {req.table} {where_clause}")
            rows = [dict(zip([col[0] for col in cur.description], row)) for row in cur.fetchall()]
            return {"status": "success", "operation": "read", "rows": rows}
        elif req.operation == "update":
            if not req.data:
                raise HTTPException(status_code=400, detail="Missing data for update operation")
            set_clause = ', '.join([f"{k}=?" for k in req.data.keys()])
            values = list(req.data.values())
            where_clause = f"WHERE {req.where}" if req.where else ""
            cur.execute(f"UPDATE {req.table} SET {set_clause} {where_clause}", values)
            conn.commit()
            return {"status": "success", "operation": "update"}
        elif req.operation == "delete":
            where_clause = f"WHERE {req.where}" if req.where else ""
            cur.execute(f"DELETE FROM {req.table} {where_clause}")
            conn.commit()
            return {"status": "success", "operation": "delete"}
        else:
            raise HTTPException(status_code=400, detail="Invalid operation")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()