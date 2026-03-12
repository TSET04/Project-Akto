import os, json, traceback
from langgraph.graph import StateGraph, END
from git import Repo
from langchain_mistralai.chat_models import ChatMistralAI
from typing import TypedDict, List, Dict
from fastapi_api_extractor import fastapi_apis
from express_api_extractor import express_apis, strategy_regex
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv

load_dotenv()

class FormattedOutput(TypedDict):
    framework: str
    endpoint: str
    method: str
    description: str
    request_schema: Dict
    response_schema: Dict

class FinalOutput(TypedDict):
    final_output: List[FormattedOutput]

class AgentState(TypedDict):
    repo_url: str
    repo_path: str

    fastapi_apis: List[Dict]
    express_apis: List[Dict]

    final_output: FinalOutput

api_key = os.getenv("MISTRAL_API_KEY")
if not api_key:
    raise ValueError("MISTRAL API KEY NOT SET")

llm = ChatMistralAI(
    api_key=api_key,
    model="mistral-large-latest",
    temperature=0
)

system_prompt = """
You are an expert backend analysis agent who specializes in understanding the core of APIs.

Your task is to understand the given information about API including the fetched code and return a structured JSON output. The given API 
file will have all the relevant and important information about API. The APIs in the file can be dynamic, route mapped or even a complex 
structure one, but everything about that API will given to you. 
Your goal is to extract the meaningful information and populate the given the output structure accordingly.

Here is the APIs file: {code}

VALID JSON OUTPUT (MANDATORY):
result: 
[
    {{
        "framework": <framework name>,
        "endpoint": <defined endpoint>,
        "method": <api method>,
        "description": <brief description of API>,
        "request_schema": <request schema of API>,
        "response_schema": <response schema of the API>,
    }}
]

GUARDRAILS:
- Do not add any other metadata or extra commentary.
- Do not add ``` or ```json either in the beginning or at the end of the output. Just return the valid JSON structure.
"""

# Helper function to tackle the LLM structured output issue
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def invoke_with_retry(batch):
    payload = {"apis": batch}

    response = llm.invoke([
        ("system", "You are an expert backend analyzing agent."),
        ("user", system_prompt.format(code=json.dumps(payload, indent=2)))
    ])

    raw = response.content.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    parsed = json.loads(raw)

    # Normalize — model might return list directly or wrapped in "result"
    if isinstance(parsed, list):
        return parsed
    return parsed.get("result") or parsed.get("final_output") or []

# Node to clone repo
def clone_repo(state: AgentState):
    try:
        url = state["repo_url"]
        path = state["repo_path"]
        
        if os.path.exists(path):
            pass
        else:
            Repo.clone_from(url, path)
    except:
        raise ValueError("ERROR OCCURED IN CLONE REPO NODE: ", traceback.print_exc())
    return {"repo_path": path}

# Node for analyzing fastapi-based APIs
def extract_fastapi_apis(state: AgentState):
    try:
        repo_dir = state["repo_path"]
        result = fastapi_apis(repo_dir)
    except:
        raise ValueError("ERROR OCCURED IN EXTRACT PYTHON API NODE: ", traceback.print_exc())
    return {"fastapi_apis": result}

# Node for analyzing express-based APIs
def extract_express_apis(state: AgentState):
    try:
        # Express API function for any generic repo 
        result = express_apis(
            repo_dir = state["repo_path"],
            resource_generators={"myorm"},
            resource_list_methods=("GET", "POST"),
            resource_detail_methods=("GET", "PUT", "PATCH", "DELETE"),
            model_name_extractor=strategy_regex(r"db\.model\('(\w+)'"),
            extra_skip_dirs={"fixtures", "codefixes", "frontend"}
        )
    except:
        raise ValueError("ERROR OCCURED IN EXTRACT EXPRESS API NODE: ", traceback.print_exc())
    
    return {"express_apis": result}

# Node for processing the combined results
def create_final_output(state: AgentState):
    endpoints = state["fastapi_apis"] + state["express_apis"]
    final_results = []
    batch_size = 9

    print("Total Endpoints to process:", len(endpoints))

    for i in range(0, len(endpoints), batch_size):
        batch = endpoints[i: i + batch_size]
        paths = [api["path"] for api in batch]
        try:
            result = invoke_with_retry(batch)
            final_results.extend(result)
            print(f"Batch {i//batch_size + 1}: Succesfully Executed -> {paths}", end="\n\n")

        except json.JSONDecodeError:
            print(f"Batch {i//batch_size + 1}: JSON Parse Failed -> {paths}", end="\n\n")
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                print(f"Batch {i//batch_size + 1}: Rate Limited -> {paths}", end="\n\n")
            else:
                traceback.print_exc()

    return {"final_output": final_results}

graph = StateGraph(AgentState)
graph.add_node("clone_repo", clone_repo)
graph.add_node("extract_fastapi_apis", extract_fastapi_apis)
graph.add_node("extract_express_apis", extract_express_apis)
graph.add_node("create_final_output", create_final_output)

graph.set_entry_point("clone_repo")

graph.add_edge("clone_repo", "extract_fastapi_apis")
graph.add_edge("clone_repo", "extract_express_apis")
graph.add_edge("extract_fastapi_apis", "create_final_output")
graph.add_edge("extract_express_apis", "create_final_output")

graph.add_edge("create_final_output", END)

workflow = graph.compile()

url = "https://github.com/juice-shop/juice-shop"

response = workflow.invoke({"repo_url": url, "repo_path": f"./repo_{str(url)[19:25]}"})
# print(response["final_output"])

# For batch size 3 it took approx 5 minutes to process 104 APIs in JUICE SHOP repo
# For batch size 5 it took around 4.5 - 5 minutes to process 104 APIs in JUICE SHOP repo
# For batch size 7 it took around 2.5 - 3 minutes to process 104 APIs in JUICE SHOP repo
# For batch size 9 it took around 2 - 2.5 minutes to process 104 APIs in JUICE SHOP repo