import os
import sys
import logging
import traceback
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from langchain_core.messages import HumanMessage, AIMessage
from pydantic import BaseModel
from typing import List, Optional
import copy

os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class UTF8StreamHandler(logging.StreamHandler):
    def __init__(self, stream=None):
        super().__init__(stream or sys.stdout)
    def emit(self, record):
        try:
            msg = self.format(record)
            self.stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

logging.basicConfig(level=logging.INFO, handlers=[UTF8StreamHandler()])

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from agent import app as langgraph_app, extract_text, selection_wait_node, get_stagehand

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    # Warm up Chrome + Stagehand in the background so the first demo search
    # starts instantly instead of waiting for the browser to launch.
    async def _warm():
        try:
            await get_stagehand()
        except Exception as e:
            print(f"[warmup] Stagehand not ready yet: {e}")
    try:
        asyncio.create_task(_warm())
    except Exception:
        pass
    yield


app = FastAPI(title="TruthPhone API", lifespan=lifespan)


def make_fresh_state():
    return {
        "messages": [],
        "requirements": {},
        "ready_to_search": False,
        "search_results": [],
        "comparison_summary": "",
        "selected_phone_index": None,
        "claims_to_check": [],
        "verification_results": [],
        "critic_synthesis": "",
        "final_report": "",
        "current_phase": "clarification",
    }


global_state = make_fresh_state()


class ChatRequest(BaseModel):
    message: str
    force_search: bool = False



@app.get("/", response_class=HTMLResponse)
async def get_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TruthPhone — AI Fact-Checker</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Outfit', sans-serif;
            background: #020617; /* Deep Slate Blue */
            background-image: 
                radial-gradient(circle at 15% 50%, rgba(37, 99, 235, 0.15), transparent 30%),
                radial-gradient(circle at 85% 30%, rgba(56, 189, 248, 0.15), transparent 30%);
            color: #e2e8f0;
            height: 100vh;
            overflow: hidden;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 2vh 20px;
        }
        .container { 
            width: 100%; 
            max-width: 900px;
            height: 100%;
            max-height: 1100px;
            background: rgba(30, 58, 138, 0.15); /* Bluish Glass */
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid rgba(147, 197, 253, 0.15); /* Light blue border */
            border-radius: 24px;
            box-shadow: 0 25px 50px -12px rgba(2, 6, 23, 0.7), inset 0 0 20px rgba(59, 130, 246, 0.05);
            padding: 30px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        header { text-align: center; margin-bottom: 10px; }
        header h1 {
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, #38bdf8, #818cf8, #c084fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }
        header p { 
            color: #a1a1aa; 
            font-size: 1rem; 
            font-weight: 300; 
            margin-top: 8px; 
            letter-spacing: 0.5px;
        }
        #chatbox {
            flex: 1;
            height: auto;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
            padding: 20px 10px;
            scroll-behavior: smooth;
        }
        #chatbox::-webkit-scrollbar { width: 6px; }
        #chatbox::-webkit-scrollbar-thumb { background: #3f3f46; border-radius: 10px; }
        .msg {
            padding: 16px 20px;
            border-radius: 18px;
            font-size: 1rem;
            max-width: 95%;
            animation: slideUp 0.4s cubic-bezier(0.16, 1, 0.3, 1);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }
        @keyframes slideUp { 
            from { opacity: 0; transform: translateY(15px) scale(0.98); } 
            to { opacity: 1; transform: translateY(0) scale(1); } 
        }
        .user {
            background: linear-gradient(135deg, #2563eb, #4f46e5);
            color: #ffffff;
            align-self: flex-end;
            border-bottom-right-radius: 4px;
        }
        .bot {
            background: rgba(30, 58, 138, 0.25);
            border: 1px solid rgba(147, 197, 253, 0.15);
            color: #e0e7ff;
            align-self: flex-start;
            border-bottom-left-radius: 4px;
        }
        .bot strong { color: #38bdf8; font-weight: 600; letter-spacing: 0.5px; }
        .bot p { margin-bottom: 8px; }
        .bot table {
            width: 100%;
            border-collapse: collapse;
            margin: 12px 0;
            background: rgba(15, 23, 42, 0.5);
            border-radius: 8px;
            overflow-x: auto;
            display: block;
            white-space: nowrap;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .bot th, .bot td {
            padding: 12px 14px;
            border: 1px solid rgba(147, 197, 253, 0.1);
            text-align: left;
            font-size: 0.95rem;
        }
        .bot th { background: rgba(37, 99, 235, 0.2); color: #ffffff; font-weight: 600; }
        .bot tr:nth-child(even) { background: rgba(255, 255, 255, 0.02); }
        .bot a { color: #60a5fa; text-decoration: none; font-weight: 600; }
        .bot a:hover { text-decoration: underline; color: #93c5fd; }
        .input-area {
            background: rgba(30, 58, 138, 0.15);
            border: 1px solid rgba(147, 197, 253, 0.15);
            border-radius: 16px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        .controls-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        /* Sleek Toggle Switch */
        .toggle-container {
            display: flex;
            align-items: center;
            cursor: pointer;
            gap: 10px;
            color: #a1a1aa;
            font-size: 0.95rem;
            font-weight: 400;
            transition: color 0.3s;
        }
        .toggle-container:hover { color: #e4e4e7; }
        .toggle-switch {
            position: relative;
            width: 44px;
            height: 24px;
            background: #3f3f46;
            border-radius: 24px;
            transition: background 0.3s;
        }
        .toggle-switch::after {
            content: '';
            position: absolute;
            top: 2px;
            left: 2px;
            width: 20px;
            height: 20px;
            background: #ffffff;
            border-radius: 50%;
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }
        #forceSearch { display: none; }
        #forceSearch:checked + .toggle-container .toggle-switch {
            background: linear-gradient(135deg, #38bdf8, #4f46e5);
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.4);
        }
        #forceSearch:checked + .toggle-container .toggle-switch::after {
            transform: translateX(20px);
        }

        .input-row {
            display: flex;
            gap: 12px;
        }
        #userInput {
            flex: 1;
            padding: 16px 20px;
            border-radius: 12px;
            border: 1px solid rgba(147, 197, 253, 0.2);
            background: rgba(15, 23, 42, 0.6);
            color: #ffffff;
            font-size: 1.05rem;
            font-family: inherit;
            outline: none;
            transition: all 0.3s;
        }
        #userInput:focus {
            border-color: #38bdf8;
            box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2);
            background: rgba(15, 23, 42, 0.9);
        }
        .btn {
            padding: 0 28px;
            border-radius: 12px;
            border: none;
            font-size: 1rem;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .btn:active { transform: scale(0.95); }
        .btn-send { 
            background: linear-gradient(135deg, #3b82f6, #818cf8); 
            color: #fff; 
            box-shadow: 0 4px 15px rgba(59, 130, 246, 0.3);
        }
        .btn-send:hover:not(:disabled) { 
            box-shadow: 0 6px 20px rgba(59, 130, 246, 0.5);
            transform: translateY(-1px);
        }
        .btn-send:disabled { opacity: 0.6; cursor: not-allowed; filter: grayscale(1); }
        .btn-reset { 
            background: transparent; 
            color: #ef4444; 
            border: 1px solid rgba(239, 68, 68, 0.3);
            padding: 6px 16px;
            font-size: 0.85rem;
        }
        .btn-reset:hover { 
            background: rgba(239, 68, 68, 0.1);
            border-color: #ef4444;
        }
        
        .typing-container {
            display: none;
            align-items: center;
            gap: 12px;
            color: #38bdf8;
            font-size: 0.9rem;
            font-weight: 600;
            padding-left: 10px;
        }
        .typing-container.active { display: flex; animation: fadeIn 0.3s; }
        .dots { display: flex; gap: 4px; }
        .dot {
            width: 6px; height: 6px;
            background: #38bdf8;
            border-radius: 50%;
            animation: bounce 1.4s infinite ease-in-out both;
        }
        .dot:nth-child(1) { animation-delay: -0.32s; }
        .dot:nth-child(2) { animation-delay: -0.16s; }
        @keyframes bounce {
            0%, 80%, 100% { transform: scale(0); }
            40% { transform: scale(1); }
        }
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>TruthPhone</h1>
        <p>Guided Smartphone Buying & AI Fact-Checking</p>
    </header>
    <div id="chatbox">
        <div class="msg bot"><strong>TruthPhone Agent:</strong><br/>Hello! I'm your AI smartphone assistant. Let me know what kind of phone you're looking for, or turn on the Web Search toggle when you're ready to scrape Amazon!</div>
    </div>
    
    <div class="input-area">
        <div class="controls-row">
            <div class="typing-container" id="typing">
                <span>Agents are researching</span>
                <div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
            </div>
            
            <input type="checkbox" id="forceSearch">
            <label for="forceSearch" class="toggle-container">
                <div class="toggle-switch"></div>
                <span>Force Web Search</span>
            </label>
        </div>
        
        <div class="input-row">
            <input type="text" id="userInput" placeholder="Ask a question, or type a search query..." autocomplete="off">
            <button class="btn btn-send" id="sendBtn" onclick="sendMessage()">Send</button>
            <button class="btn btn-reset" onclick="resetChat()">Reset</button>
        </div>
    </div>
</div>
<script>
    const chatbox = document.getElementById('chatbox');
    const input = document.getElementById('userInput');
    const sendBtn = document.getElementById('sendBtn');
    const typing = document.getElementById('typing');

    input.addEventListener('keypress', (e) => { if (e.key === 'Enter') sendMessage(); });

    function appendMessage(sender, text, cls) {
        const div = document.createElement('div');
        div.className = 'msg ' + cls;
        if (cls === 'user') {
            div.textContent = text;
        } else {
            div.innerHTML = '<strong>' + sender + ':</strong><br/>' + marked.parse(text);
        }
        chatbox.appendChild(div);
        setTimeout(() => chatbox.scrollTop = chatbox.scrollHeight, 50);
    }

    async function sendMessage() {
        const msg = input.value.trim();
        if (!msg) return;

        appendMessage('You', msg, 'user');
        input.value = '';
        sendBtn.disabled = true;
        typing.classList.add('active');
        const forceSearch = document.getElementById('forceSearch').checked;

        try {
            const resp = await fetch('/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: msg, force_search: forceSearch })
            });
            const data = await resp.json();
            if (data.response) {
                appendMessage('TruthPhone Agent', data.response, 'bot');
            } else if (data.error) {
                appendMessage('Error', data.error, 'bot');
            }
        } catch (err) {
            appendMessage('Error', 'Network error: ' + err.message, 'bot');
        } finally {
            sendBtn.disabled = false;
            typing.classList.remove('active');
            // Uncheck the toggle after use so it resets to conversation mode
            if (forceSearch) document.getElementById('forceSearch').checked = false;
        }
    }

    async function resetChat() {
        await fetch('/reset', { method: 'POST' });
        chatbox.innerHTML = '<div class="msg bot"><strong>TruthPhone Agent:</strong><br/>Hello! I\\'m your AI smartphone assistant. Let me know what kind of phone you\\'re looking for, or turn on the Web Search toggle when you\\'re ready to scrape Amazon!</div>';
    }
</script>
</body>
</html>"""


@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    global global_state

    try:
        global_state["messages"].append(HumanMessage(content=req.message))

        # ── Global Override: Force Web Search ──────────────────────────────
        import re
        is_just_number = bool(re.match(r'^\d+$', req.message.strip()))

        if req.force_search and not is_just_number:
            print("⚡ USER FORCED NEW AMAZON WEB SEARCH VIA UI TOGGLE")
            global_state["messages"].append(AIMessage(content="User requested a new search. 🔍 Searching live Amazon listings for you now…"))
            global_state["ready_to_search"] = True
            global_state["current_phase"] = "search"
            # It will now fall through and run the graph

        # ── Phase: Selection (user picking a phone number) ─────────────
        elif global_state.get("current_phase") in ["selection", "verification"]:
            # Check if they typed a valid number
            digits = [s for s in req.message.split() if s.isdigit()]
            if digits:
                selection = int(digits[0]) - 1
                if 0 <= selection < len(global_state.get("search_results", [])):
                    global_state["selected_phone_index"] = selection
                    
                    from agent import verification_planner_node, verifier_node, critic_node, finalizer_node
                    
                    res = await verification_planner_node(global_state)
                    global_state.update(res)
                    
                    res = await verifier_node(global_state)
                    global_state.update(res)
                    
                    res = await critic_node(global_state)
                    global_state.update(res)
                    
                    res = await finalizer_node(global_state)
                    global_state.update(res)
                    
                    return {"response": _get_latest_ai_text(global_state)}
            
            # If they didn't type a number, just answer their question conversationally!
            from agent import llm
            gen_response = await llm.ainvoke(global_state["messages"])
            return {"response": extract_text(gen_response.content)}

        # ── All other phases: run the main graph ───────────────────────
        try:
            final_state = await langgraph_app.ainvoke(global_state)
            global_state.update(final_state)
        except Exception as graph_err:
            if "tool" in str(graph_err).lower():
                # If the AI hallucinates a tool call, silently retry once with a pure text prompt
                global_state["messages"][-1] = HumanMessage(content=req.message + "\n\n(System Note: Please answer in pure text only. Do not use tools.)")
                final_state = await langgraph_app.ainvoke(global_state)
                global_state.update(final_state)
            else:
                raise graph_err

        # If we've reached comparison, return the comparison summary
        if final_state.get("current_phase") == "selection" and final_state.get("comparison_summary"):
            return {"response": final_state["comparison_summary"]}

        return {"response": _get_latest_ai_text(final_state)}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=200,
            content={"response": f"⚠️ Something went wrong: {str(e)}. Please click Reset and try again."}
        )


def _get_latest_ai_text(state: dict) -> str:
    """Extract the latest AI message text from state."""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            return extract_text(m.content)
    return "No response generated."


@app.post("/reset")
async def reset_endpoint():
    global global_state
    global_state = make_fresh_state()
    return {"status": "ok"}


if __name__ == "__main__":
    import sys
    # NOTE: reload=True makes uvicorn force a SelectorEventLoop on Windows, which
    # cannot spawn subprocesses. Stagehand needs to launch Chrome, so reload is off.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
