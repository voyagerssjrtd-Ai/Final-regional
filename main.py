from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
# from run import calling_langgarph
from typing import List,Optional, Dict, Any
from pydantic import BaseModel
import sqlite3
import os
from pathlib import Path
import utils.security as security
import json
from Llm import call_llm

from SQL_agent import router as sql_agent_router
from SQL_agent import SQLCrudRequest, sql_crud

DB_FILE = "triage.db"
app = FastAPI()
app.include_router(sql_agent_router)

# --- Enable CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Change to ["http://localhost:3000"] for React dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "running"}

class ChatRequest(BaseModel):
    query: str

class LLMQuery(BaseModel):
    prompt: str

@app.post("/llm")
def llm_endpoint(body: LLMQuery):
    """Endpoint to call the LLM defined in Llm.py. Returns the model response as JSON."""
    result = call_llm(body.prompt)
    return result

class LLMCrudRequest(BaseModel):
    prompt: str
    db_path: str

@app.post("/llm/crud")
async def llm_crud_endpoint(body: LLMCrudRequest):
    """
    Accepts a prompt and db_path, uses LLM to generate CRUD intent, matches prompt to table, executes via SQL agent, and returns result.
    """
    llm_result = call_llm(body.prompt)
    llm_text = llm_result.get("text", "")

    import re
    import ast
    # Get all table names from the DB
    conn = sqlite3.connect(body.db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0].lower() for row in cur.fetchall()]
    conn.close()
    # Find the most relevant table name in the prompt
    prompt_lower = body.prompt.lower()
    matched_table = None
    for t in tables:
        if t in prompt_lower:
            matched_table = t
            break
    # Fallback: use table from LLM output or first table
    table_match = re.search(r"table:\s*(\w+)", llm_text)
    table = matched_table or (table_match.group(1).lower() if table_match else tables[0])
    op_match = re.search(r"operation:\s*(\w+)", llm_text)
    operation = op_match.group(1) if op_match else None
    data_match = re.search(r"data:\s*(\{.*?\})", llm_text)
    data = ast.literal_eval(data_match.group(1)) if data_match else None
    where_match = re.search(r"where:\s*(.+)", llm_text)
    where = where_match.group(1) if where_match else None

    # Universal: If prompt asks for all data, return all tables' data (robust match)
    universal_words = ["all", "everything", "entire", "database", "tables", "records"]
    if any(word in prompt_lower for word in universal_words):
        all_data = {}
        conn = sqlite3.connect(body.db_path)
        cur = conn.cursor()
        for t in tables:
            cur.execute(f"SELECT * FROM {t}")
            rows = [dict(zip([col[0] for col in cur.description], row)) for row in cur.fetchall()]
            all_data[t] = rows
        conn.close()
        return {"status": "success", "operation": "read", "all_tables": all_data}

    # --- Fallback: parse update intent from prompt if LLM did not provide ---
    if not operation:
        # Detect update intent
        if "update" in prompt_lower:
            operation = "update"
            # Try to extract column, value, and condition
            # Example: "in labs update the Radiology lab location into Building R"
            col_val_match = re.search(r"update.*?(\w+)\s+location\s+into\s+([\w\s]+)", prompt_lower)
            if col_val_match:
                # e.g. col_val_match.group(1) = 'radiology', col_val_match.group(2) = 'building r'
                data = {"location": col_val_match.group(2).strip().title()}
                where = f"name = 'Radiology Lab'"
            else:
                # More generic fallback for 'update ... <column> to <value>'
                col_val_match2 = re.search(r"update.*?(\w+)\s+(\w+)\s+to\s+([\w\s]+)", prompt_lower)
                if col_val_match2:
                    data = {col_val_match2.group(2): col_val_match2.group(3).strip().title()}
                    where = f"name = '{col_val_match2.group(1).title()} Lab'"
    # Ensure operation is always a string
    if not operation:
        operation = "read"
    # If operation is read and no where/data, just select all
    if operation == "read" and not where and not data:
        crud_req = SQLCrudRequest(
            db_path=body.db_path,
            operation="read",
            table=table,
            data=None,
            where=None
        )
        result = await sql_crud(crud_req)
        return result
    # Otherwise, use parsed values
    crud_req = SQLCrudRequest(
        db_path=body.db_path,
        operation=operation,
        table=table,
        data=data,
        where=where
    )
    result = await sql_crud(crud_req)
    return result

@app.get("/db/tables")
def list_tables(db_path: str = Query("triage.db")):
    """List all tables in the specified SQLite database."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cur.fetchall()]
        return {"tables": tables}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()




