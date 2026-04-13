"""
LLM-enabled MCP server — natural language → PST operations.

Tools exposed:
  pst_agent              — natural-language dispatcher (LLM picks the right tool)
  extract_pst            — full extract of all emails + contacts
  draft_email            — compose an unsent draft MSG/EML
  search_emails_by_sender  — find all emails from a given sender address/name
  get_latest_emails      — retrieve the N most recent emails (sorted by date)
  list_pst_folders       — show folder tree with item counts
  search_emails_by_subject — find emails whose subject contains a keyword

Depends on:
  pip install fastmcp python-dotenv langchain-nvidia-ai-endpoints colorama
  plus Aspose.Email-for-Python-via-NET

Run:
  python extract_pst_mcp_server_llm.py

Default URL: http://0.0.0.0:9003/mcp

Environment:
  NVIDIA_API_KEY             — required for ChatNVIDIA
  ASPOSE_EMAIL_LICENSE_PATH  — optional .lic file
  MCP_EXTRACT_PST_HOST       — bind host  (default 0.0.0.0)
  MCP_EXTRACT_PST_PORT       — bind port  (default 9003)
  MCP_EXTRACT_PST_PATH       — URL path   (default /mcp)
  MCP_EXTRACT_PST_LOG_LEVEL  — log level  (default debug)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

_EXAMPLES = Path(__file__).resolve().parent
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from colorama import Fore, init as colorama_init
from fastmcp import FastMCP
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_nvidia_ai_endpoints import ChatNVIDIA

import extract_pst_emails_and_contacts as pst_lib

colorama_init(autoreset=True)
DEFAULT_PST = str(_EXAMPLES / "Data" / "Outlook.pst")


# ── LLM setup (same model as memory_mcp_server.py) ───────────────────────────
llm = ChatNVIDIA(model="nvidia/llama-3.3-nemotron-super-49b-v1.5")

mcp = FastMCP("ExtractPstMCP_LLM")


# ── shared helpers ────────────────────────────────────────────────────────────

def strip_think_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _safe(v) -> str:
    return "" if v is None else str(v)


def _truncate(text: str, n: int = 400) -> str:
    text = text.replace("\r\n", "\n").strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def _format_message(mapi, idx: int, folder_name: str) -> str:
    lines = [
        f"--- Email #{idx} | folder: {folder_name} ---",
        f"Subject : {_safe(mapi.subject)}",
        f"From    : {_safe(mapi.sender_name)} <{_safe(mapi.sender_email_address)}>",
        f"To      : {_safe(mapi.display_to)}",
    ]
    if _safe(mapi.display_cc):
        lines.append(f"Cc      : {_safe(mapi.display_cc)}")
    lines.append(f"Date    : {_safe(mapi.delivery_time)}")
    body = _safe(mapi.body)
    if body:
        lines.append(f"Body    :\n{_truncate(body)}")
    lines.append("")
    return "\n".join(lines)


# ── PST query functions (sync — called via asyncio.to_thread) ─────────────────

def _search_by_sender_sync(
    pst_path: str,
    sender: str,
    max_results: int,
    folder_name: Optional[str],
) -> str:
    from aspose.email.storage.pst import PersonalStorage, PersonalStorageQueryBuilder

    pst_lib._apply_license(os.environ.get("ASPOSE_EMAIL_LICENSE_PATH"))
    buf = io.StringIO()

    with PersonalStorage.from_file(pst_path, False) as store:
        # Decide which folders to search
        if folder_name:
            folders = [store.root_folder.get_sub_folder(folder_name)]
            if folders[0] is None:
                return f"Folder '{folder_name}' not found in PST."
        else:
            folders = list(store.root_folder.get_sub_folders())
            folders.insert(0, store.root_folder)

        qb = PersonalStorageQueryBuilder()
        qb.from_address.contains(sender, True)   # True = ignore case
        query = qb.get_query()

        found = 0
        for folder in folders:
            if max_results and found >= max_results:
                break
            try:
                messages = folder.get_contents(query)
            except Exception:
                # folder might not support query — fall back to full scan of this folder
                messages = []
            for info in messages:
                if max_results and found >= max_results:
                    break
                try:
                    mapi = store.extract_message(info)
                except Exception as ex:
                    buf.write(f"[skip] {ex}\n")
                    continue
                if not pst_lib._is_likely_mail(mapi):
                    continue
                found += 1
                buf.write(_format_message(mapi, found, _safe(folder.display_name)))

            # also recurse into sub-folders if searching all
            if not folder_name and folder.has_sub_folders:
                for sub in folder.get_sub_folders():
                    if max_results and found >= max_results:
                        break
                    try:
                        sub_msgs = sub.get_contents(query)
                    except Exception:
                        sub_msgs = []
                    for info in sub_msgs:
                        if max_results and found >= max_results:
                            break
                        try:
                            mapi = store.extract_message(info)
                        except Exception as ex:
                            buf.write(f"[skip] {ex}\n")
                            continue
                        if not pst_lib._is_likely_mail(mapi):
                            continue
                        found += 1
                        buf.write(_format_message(mapi, found, _safe(sub.display_name)))

    if found == 0:
        return f"No emails found from sender matching '{sender}'."
    header = f"Found {found} email(s) from '{sender}'"
    if max_results and found >= max_results:
        header += f" (stopped at limit {max_results})"
    return header + "\n\n" + buf.getvalue()


def _get_latest_emails_sync(
    pst_path: str,
    count: int,
    folder_name: Optional[str],
) -> str:
    from aspose.email.storage.pst import PersonalStorage

    pst_lib._apply_license(os.environ.get("ASPOSE_EMAIL_LICENSE_PATH"))

    # Collect (delivery_time, mapi, folder_name) across all target folders
    collected: List[Tuple] = []

    def _walk(folder, store):
        try:
            messages = folder.get_contents()
        except Exception:
            return
        for info in messages:
            try:
                mapi = store.extract_message(info)
            except Exception:
                continue
            if not pst_lib._is_likely_mail(mapi):
                continue
            dt = mapi.delivery_time  # datetime or None
            collected.append((dt, mapi, _safe(folder.display_name)))
        if folder.has_sub_folders:
            for sub in folder.get_sub_folders():
                _walk(sub, store)

    with PersonalStorage.from_file(pst_path, False) as store:
        if folder_name:
            target = store.root_folder.get_sub_folder(folder_name)
            if target is None:
                return f"Folder '{folder_name}' not found in PST."
            _walk(target, store)
        else:
            _walk(store.root_folder, store)

    if not collected:
        return "No emails found in PST."

    # Sort descending by delivery_time (None values go to the end)
    collected.sort(key=lambda x: x[0] if x[0] is not None else "", reverse=True)
    top = collected[:count]

    buf = io.StringIO()
    buf.write(f"Latest {len(top)} email(s) (sorted by date desc):\n\n")
    for idx, (_, mapi, fname) in enumerate(top, 1):
        buf.write(_format_message(mapi, idx, fname))
    return buf.getvalue()


def _list_folders_sync(pst_path: str) -> str:
    from aspose.email.storage.pst import PersonalStorage

    pst_lib._apply_license(os.environ.get("ASPOSE_EMAIL_LICENSE_PATH"))
    buf = io.StringIO()

    def _walk(folder, depth: int):
        indent = "  " * depth
        total = getattr(folder, "content_count", "?")
        unread = getattr(folder, "content_unread_count", "?")
        buf.write(f"{indent}[{_safe(folder.display_name)}]  total={total}  unread={unread}\n")
        if folder.has_sub_folders:
            for sub in folder.get_sub_folders():
                _walk(sub, depth + 1)

    with PersonalStorage.from_file(pst_path, False) as store:
        store_name = _safe(getattr(store.store, "display_name", "")) or pst_path
        buf.write(f"PST store: {store_name}\n\n")
        _walk(store.root_folder, 0)

    return buf.getvalue()


def _search_by_subject_sync(
    pst_path: str,
    keyword: str,
    max_results: int,
) -> str:
    from aspose.email.storage.pst import PersonalStorage, PersonalStorageQueryBuilder

    pst_lib._apply_license(os.environ.get("ASPOSE_EMAIL_LICENSE_PATH"))
    buf = io.StringIO()

    qb = PersonalStorageQueryBuilder()
    qb.subject.contains(keyword, True)   # True = ignore case
    query = qb.get_query()

    found = 0

    def _walk(folder, store):
        nonlocal found
        if max_results and found >= max_results:
            return
        try:
            messages = folder.get_contents(query)
        except Exception:
            messages = []
        for info in messages:
            if max_results and found >= max_results:
                return
            try:
                mapi = store.extract_message(info)
            except Exception as ex:
                buf.write(f"[skip] {ex}\n")
                continue
            if not pst_lib._is_likely_mail(mapi):
                continue
            found += 1
            buf.write(_format_message(mapi, found, _safe(folder.display_name)))
        if folder.has_sub_folders:
            for sub in folder.get_sub_folders():
                _walk(sub, store)

    with PersonalStorage.from_file(pst_path, False) as store:
        _walk(store.root_folder, store)

    if found == 0:
        return f"No emails found with subject containing '{keyword}'."
    header = f"Found {found} email(s) with subject containing '{keyword}'"
    if max_results and found >= max_results:
        header += f" (stopped at limit {max_results})"
    return header + "\n\n" + buf.getvalue()


def _search_by_date_range_sync(
    pst_path: str,
    start_date: str,
    end_date: str,
    max_results: int,
    folder_name: Optional[str],
) -> str:
    """Return emails whose delivery_time falls within [start_date, end_date].

    Dates are parsed from ISO-8601 strings (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).
    The range is inclusive on both ends (start defaults to 00:00:00, end to 23:59:59
    when only a date is supplied).
    """
    from aspose.email.storage.pst import PersonalStorage, PersonalStorageQueryBuilder

    def _parse(s: str, end_of_day: bool = False) -> datetime:
        s = s.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%Y-%m-%d" and end_of_day:
                    dt = dt.replace(hour=23, minute=59, second=59)
                return dt
            except ValueError:
                continue
        raise ValueError(
            f"Cannot parse date {s!r}. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
        )

    try:
        dt_start = _parse(start_date, end_of_day=False)
        dt_end   = _parse(end_date,   end_of_day=True)
    except ValueError as exc:
        return f"Error: {exc}"

    if dt_start > dt_end:
        return "Error: start_date must be earlier than or equal to end_date."

    pst_lib._apply_license(os.environ.get("ASPOSE_EMAIL_LICENSE_PATH"))
    buf = io.StringIO()

    qb = PersonalStorageQueryBuilder()
    qb.delivery_time.since(dt_start)
    qb.delivery_time.before(dt_end)
    query = qb.get_query()

    found = 0

    def _walk(folder, store):
        nonlocal found
        if max_results and found >= max_results:
            return
        try:
            messages = folder.get_contents(query)
        except Exception:
            messages = []
        for info in messages:
            if max_results and found >= max_results:
                return
            try:
                mapi = store.extract_message(info)
            except Exception as ex:
                buf.write(f"[skip] {ex}\n")
                continue
            if not pst_lib._is_likely_mail(mapi):
                continue
            found += 1
            buf.write(_format_message(mapi, found, _safe(folder.display_name)))
        if folder.has_sub_folders:
            for sub in folder.get_sub_folders():
                _walk(sub, store)

    with PersonalStorage.from_file(pst_path, False) as store:
        if folder_name:
            target = store.root_folder.get_sub_folder(folder_name)
            if target is None:
                return f"Folder '{folder_name}' not found in PST."
            _walk(target, store)
        else:
            _walk(store.root_folder, store)

    if found == 0:
        return f"No emails found between {start_date} and {end_date}."
    header = f"Found {found} email(s) between {start_date} and {end_date}"
    if max_results and found >= max_results:
        header += f" (stopped at limit {max_results})"
    return header + "\n\n" + buf.getvalue()


def _count_emails_sync(pst_path: str) -> str:
    """Walk the PST folder tree and count email items per folder plus a grand total."""
    from aspose.email.storage.pst import PersonalStorage

    pst_lib._apply_license(os.environ.get("ASPOSE_EMAIL_LICENSE_PATH"))
    buf = io.StringIO()
    grand_total = 0

    def _walk(folder, depth: int) -> int:
        indent = "  " * depth
        folder_count = 0
        try:
            messages = folder.get_contents()
        except Exception:
            messages = []
        for info in messages:
            try:
                mapi = folder  # just counting via info iteration
                _ = info       # info itself is the MessageInfo object
                folder_count += 1
            except Exception:
                continue
        sub_count = 0
        if folder.has_sub_folders:
            for sub in folder.get_sub_folders():
                sub_count += _walk(sub, depth + 1)
        total_here = folder_count + sub_count
        buf.write(
            f"{indent}[{_safe(folder.display_name)}]  "
            f"direct={folder_count}  subtree={total_here}\n"
        )
        return total_here

    with PersonalStorage.from_file(pst_path, False) as store:
        store_name = _safe(getattr(store.store, "display_name", "")) or pst_path
        buf.write(f"PST: {store_name}\n\n")

        root = store.root_folder
        # count root-level messages
        try:
            root_msgs = list(root.get_contents())
        except Exception:
            root_msgs = []
        root_direct = len(root_msgs)

        sub_total = 0
        if root.has_sub_folders:
            for sub in root.get_sub_folders():
                sub_total += _walk(sub, 1)

        grand_total = root_direct + sub_total
        buf.write(f"\nGrand total emails: {grand_total}\n")

    return buf.getvalue()


# ── LLM intent parser ─────────────────────────────────────────────────────────

_INTENT_SYSTEM_PROMPT = """\
You are a parameter-extraction assistant for an Outlook PST email tool.
the mails had been supplied to you by exported to a pst file and the absolute path to the pst file has been set so just assume the pst file exists by default. 
Given a natural-language request return ONLY a valid JSON object — no markdown,
no explanation, no extra text.

The JSON must have exactly two keys:
  "tool"  — one of the tool names listed below
  "args"  — an object with the parameters for that tool

TOOLS AND THEIR PARAMETERS
───────────────────────────

"extract_pst"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  max_emails   (integer, optional)
  max_contacts (integer, optional)
  → Use when: user wants a general dump / full extract of emails and contacts.

"search_emails_by_sender"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  sender       (string) — email address or name fragment to search for
  max_results  (integer, optional, default 50)
  folder_name  (string, optional) — search only this folder; omit to search all
  → Use when: user asks for emails FROM a specific person or address
    e.g. "find all mails sent by saqib.razzaq@xp.local"

"get_latest_emails"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  count        (integer) — how many recent emails to return (default 10)
  folder_name  (string, optional) — limit to this folder; omit for all folders
  → Use when: user asks for the newest / latest / most recent emails
    e.g. "show me the latest 10 emails", "get the 5 newest messages"

"list_pst_folders"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  → Use when: user wants to see the folder structure / folder list

"search_emails_by_subject"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  keyword      (string) — word or phrase to look for in the subject line
  max_results  (integer, optional, default 50)
  → Use when: user wants emails about a topic / with certain words in the subject
    e.g. "find emails about project kickoff"

"get_emails_by_date_range"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  start_date   (string) — start of range, ISO-8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (inclusive)
  end_date     (string) — end of range, ISO-8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (inclusive)
  max_results  (integer, optional, default 100)
  folder_name  (string, optional) — search only this folder; omit to search all
  → Use when: user asks for emails within a time window, date range, between two dates,
    or during a specific month/year/period.
    e.g. "show emails from January 2024"
         "find all emails between 2024-03-01 and 2024-03-31"
         "get emails received last quarter"

"count_emails"
  pst_path     (string) — absolute path to the .pst file; empty string if not mentioned
  → Use when: user wants to know how many emails are in the PST, asks for a total count,
    or wants per-folder email counts.
    e.g. "how many emails are in the PST?"
         "count all emails"
         "how many messages does this mailbox have?"

"draft_email"
  subject        (string, optional)
  body           (string, optional)
  to_addresses   (string, optional) — comma-separated
  cc_addresses   (string, optional)
  bcc_addresses  (string, optional)
  from_address   (string, optional)
  body_file      (string, optional)
  out_path       (string, optional) — where to save the .msg/.eml
  file_format    (string, optional) — "msg" or "eml"
  append_to_pst  (string, optional) — PST path to append the draft to
  → Use when: user wants to compose / write / draft / send an email

Rules:
- Never invent a pst_path; use an empty string if none is mentioned.
- Omit optional keys entirely when not mentioned.
- Return ONLY the JSON object."""


def _llm_parse_intent(natural_language: str) -> dict:
    response = llm.invoke([
        SystemMessage(content=_INTENT_SYSTEM_PROMPT),
        HumanMessage(content=natural_language),
    ])
    raw = strip_think_tags(response.content.strip())
    print(Fore.YELLOW + f"[pst_agent] LLM raw: {raw}")
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise ValueError(f"LLM did not return valid JSON: {raw!r}")
    return json.loads(json_match.group())


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def pst_agent(query: str) -> str:
    """Natural-language interface to all PST operations.

    Accepts a plain-English request, uses an LLM to identify the intent and
    extract parameters, then dispatches automatically to the correct tool:
    extract_pst, search_emails_by_sender, get_latest_emails, list_pst_folders,
    search_emails_by_subject, get_emails_by_date_range, or draft_email.

    Args:
        query: e.g. "Extract the latest 10 emails from /data/mailbox.pst"
               "Find all emails sent by saqib.razzaq@xp.local"
               "Show me the folder structure of /data/mailbox.pst"
               "Find emails with 'project kickoff' in the subject"
               "Show emails received between 2024-01-01 and 2024-03-31"
               "Draft an email to alice@example.com and save to /tmp/draft.msg"
    """
    try:
        parsed = await asyncio.to_thread(_llm_parse_intent, query)
    except Exception as ex:
        return f"Error (intent parsing): {type(ex).__name__}: {ex}"

    tool_name = parsed.get("tool", "")
    args = parsed.get("args", {})
    print(Fore.CYAN + f"[pst_agent] tool={tool_name!r}  args={args}")

    pst_path = args.get("pst_path") or DEFAULT_PST

    if tool_name == "extract_pst":
        try:
            return await asyncio.to_thread(
                pst_lib.run_extract_to_string,
                pst_path,
                args.get("max_emails"),
                args.get("max_contacts"),
            )
        except Exception as ex:
            return f"Error (extract_pst): {type(ex).__name__}: {ex}"

    elif tool_name == "search_emails_by_sender":
        sender = args.get("sender", "")
        if not sender:
            return "Error: 'sender' is required for search_emails_by_sender."
        try:
            return await asyncio.to_thread(
                _search_by_sender_sync,
                pst_path,
                sender,
                args.get("max_results", 50),
                args.get("folder_name"),
            )
        except Exception as ex:
            return f"Error (search_emails_by_sender): {type(ex).__name__}: {ex}"

    elif tool_name == "get_latest_emails":
        try:
            return await asyncio.to_thread(
                _get_latest_emails_sync,
                pst_path,
                args.get("count", 10),
                args.get("folder_name"),
            )
        except Exception as ex:
            return f"Error (get_latest_emails): {type(ex).__name__}: {ex}"

    elif tool_name == "list_pst_folders":
        try:
            return await asyncio.to_thread(_list_folders_sync, pst_path)
        except Exception as ex:
            return f"Error (list_pst_folders): {type(ex).__name__}: {ex}"

    elif tool_name == "search_emails_by_subject":
        keyword = args.get("keyword", "")
        if not keyword:
            return "Error: 'keyword' is required for search_emails_by_subject."
        try:
            return await asyncio.to_thread(
                _search_by_subject_sync,
                pst_path,
                keyword,
                args.get("max_results", 50),
            )
        except Exception as ex:
            return f"Error (search_emails_by_subject): {type(ex).__name__}: {ex}"

    elif tool_name == "get_emails_by_date_range":
        start_date = args.get("start_date", "")
        end_date   = args.get("end_date", "")
        if not start_date or not end_date:
            return "Error: 'start_date' and 'end_date' are required for get_emails_by_date_range."
        try:
            return await asyncio.to_thread(
                _search_by_date_range_sync,
                pst_path,
                start_date,
                end_date,
                args.get("max_results", 100),
                args.get("folder_name"),
            )
        except Exception as ex:
            return f"Error (get_emails_by_date_range): {type(ex).__name__}: {ex}"

    elif tool_name == "count_emails":
        try:
            return await asyncio.to_thread(_count_emails_sync, pst_path)
        except Exception as ex:
            return f"Error (count_emails): {type(ex).__name__}: {ex}"

    elif tool_name == "draft_email":
        out_path = args.get("out_path")
        append_to = args.get("append_to_pst")
        if not out_path and not append_to:
            return (
                "Error: specify a save destination — e.g. "
                "'save to /tmp/draft.msg' or 'append to /data/mailbox.pst'."
            )
        try:
            return await asyncio.to_thread(
                pst_lib.run_draft_to_string,
                args.get("subject", ""),
                args.get("body", ""),
                args.get("body_file"),
                args.get("to_addresses", ""),
                args.get("cc_addresses") or None,
                args.get("bcc_addresses") or None,
                args.get("from_address"),
                out_path,
                (args.get("file_format") or "msg").lower(),
                append_to,
            )
        except Exception as ex:
            return f"Error (draft_email): {type(ex).__name__}: {ex}"

    else:
        return (
            f"Error: unrecognised tool '{tool_name}'. "
            "Valid: extract_pst, search_emails_by_sender, get_latest_emails, "
            "list_pst_folders, search_emails_by_subject, get_emails_by_date_range, "
            "count_emails, draft_email."
        )


@mcp.tool()
async def extract_pst(
    pst_path: str = "",
    max_emails: Optional[int] = None,
    max_contacts: Optional[int] = None,
) -> str:
    """Full extract of all emails and contacts from an Outlook PST file.

    Args:
        pst_path: Absolute path to the .pst file (defaults to built-in sample).
        max_emails: Stop after this many emails.
        max_contacts: Stop after this many contacts.
    """
    try:
        return await asyncio.to_thread(
            pst_lib.run_extract_to_string,
            pst_path or DEFAULT_PST,
            max_emails,
            max_contacts,
        )
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def search_emails_by_sender(
    sender: str,
    pst_path: str = "",
    max_results: int = 50,
    folder_name: Optional[str] = None,
) -> str:
    """Find all emails sent by a specific address or name (case-insensitive).

    Uses PersonalStorageQueryBuilder.from_address to filter messages efficiently.

    Args:
        sender: Email address or name fragment, e.g. "saqib.razzaq@xp.local" or "saqib".
        pst_path: Absolute path to the .pst file (defaults to built-in sample).
        max_results: Maximum number of matching emails to return (default 50).
        folder_name: Search only this folder name (e.g. "Inbox"). Omit to search all folders.
    """
    try:
        return await asyncio.to_thread(
            _search_by_sender_sync,
            pst_path or DEFAULT_PST,
            sender,
            max_results,
            folder_name,
        )
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def get_latest_emails(
    count: int = 10,
    pst_path: str = "",
    folder_name: Optional[str] = None,
) -> str:
    """Retrieve the N most recent emails, sorted by delivery date descending.

    Args:
        count: Number of recent emails to return (default 10).
        pst_path: Absolute path to the .pst file (defaults to built-in sample).
        folder_name: Limit search to this folder (e.g. "Inbox"). Omit for all folders.
    """
    try:
        return await asyncio.to_thread(
            _get_latest_emails_sync,
            pst_path or DEFAULT_PST,
            count,
            folder_name,
        )
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def list_pst_folders(pst_path: str = "") -> str:
    """List the complete folder tree of a PST with item counts per folder.

    Args:
        pst_path: Absolute path to the .pst file (defaults to built-in sample).
    """
    try:
        return await asyncio.to_thread(_list_folders_sync, pst_path or DEFAULT_PST)
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def search_emails_by_subject(
    keyword: str,
    pst_path: str = "",
    max_results: int = 50,
) -> str:
    """Find emails whose subject line contains a keyword (case-insensitive).

    Uses PersonalStorageQueryBuilder.subject to filter messages efficiently.

    Args:
        keyword: Word or phrase to search for in the subject, e.g. "project kickoff".
        pst_path: Absolute path to the .pst file (defaults to built-in sample).
        max_results: Maximum number of matching emails to return (default 50).
    """
    try:
        return await asyncio.to_thread(
            _search_by_subject_sync,
            pst_path or DEFAULT_PST,
            keyword,
            max_results,
        )
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def get_emails_by_date_range(
    start_date: str,
    end_date: str,
    pst_path: str = "",
    max_results: int = 100,
    folder_name: Optional[str] = None,
) -> str:
    """Fetch emails whose delivery date falls within a specific date range.

    Uses PersonalStorageQueryBuilder delivery_time filters (since / before) for
    efficient server-side filtering rather than loading every message.

    Args:
        start_date: Start of the date range (inclusive). ISO-8601 format:
                    YYYY-MM-DD  — treated as midnight (00:00:00) of that day.
                    YYYY-MM-DDTHH:MM:SS  — exact timestamp.
                    e.g. "2024-01-01" or "2024-01-01T08:00:00"
        end_date:   End of the date range (inclusive). ISO-8601 format:
                    YYYY-MM-DD  — treated as 23:59:59 of that day.
                    YYYY-MM-DDTHH:MM:SS  — exact timestamp.
                    e.g. "2024-03-31" or "2024-03-31T17:30:00"
        pst_path:   Absolute path to the .pst file (defaults to built-in sample).
        max_results: Maximum number of matching emails to return (default 100).
        folder_name: Search only this folder (e.g. "Inbox"). Omit to search all folders.
    """
    try:
        return await asyncio.to_thread(
            _search_by_date_range_sync,
            pst_path or DEFAULT_PST,
            start_date,
            end_date,
            max_results,
            folder_name,
        )
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def count_emails(pst_path: str = "") -> str:
    """Count the total number of email items in the PST, broken down by folder.

    Uses folder.get_contents() to count messages per folder and walks the full
    folder tree, reporting direct item counts and subtree totals alongside a
    grand total across the entire mailbox.

    Args:
        pst_path: Absolute path to the .pst file (defaults to built-in sample).
    """
    try:
        return await asyncio.to_thread(_count_emails_sync, pst_path or DEFAULT_PST)
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


@mcp.tool()
async def draft_email(
    subject: str = "",
    body: str = "",
    to_addresses: str = "",
    cc_addresses: str = "",
    bcc_addresses: str = "",
    from_address: Optional[str] = None,
    body_file: Optional[str] = None,
    out_path: Optional[str] = None,
    file_format: str = "msg",
    append_to_pst: Optional[str] = None,
) -> str:
    """Create an unsent draft email (MSG/EML file and/or add to PST Drafts folder).

    Args:
        subject: Message subject.
        body: Plain-text body (ignored if body_file is set).
        to_addresses: Comma-separated To addresses.
        cc_addresses: Comma-separated Cc addresses.
        bcc_addresses: Comma-separated Bcc addresses.
        from_address: Optional From address.
        body_file: Path to a UTF-8 file on the server to use as body.
        out_path: Save draft to this file path (.msg or .eml).
        file_format: "msg" or "eml" (default "msg").
        append_to_pst: Also append the draft to this PST's Drafts folder.
    """
    if not out_path and not append_to_pst:
        return "Error: specify at least one of out_path or append_to_pst."
    fmt = (file_format or "msg").lower()
    if fmt not in ("msg", "eml"):
        return f"Error: file_format must be msg or eml, got {file_format!r}"
    try:
        return await asyncio.to_thread(
            pst_lib.run_draft_to_string,
            subject,
            body,
            body_file,
            to_addresses,
            cc_addresses or None,
            bcc_addresses or None,
            from_address,
            out_path,
            fmt,
            append_to_pst,
        )
    except Exception as ex:
        return f"Error: {type(ex).__name__}: {ex}"


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host      = os.environ.get("MCP_EXTRACT_PST_HOST",      "0.0.0.0")
    port      = int(os.environ.get("MCP_EXTRACT_PST_PORT",  "9003"))
    path      = os.environ.get("MCP_EXTRACT_PST_PATH",      "/mcp")
    log_level = os.environ.get("MCP_EXTRACT_PST_LOG_LEVEL", "debug")

    asyncio.run(
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            path=path,
            log_level=log_level,
        )
    )
