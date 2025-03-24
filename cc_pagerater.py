import os
import requests
import logging
import toml
import hashlib
import json
import time
import sqlite3
from dotenv import load_dotenv
from types import MappingProxyType
from atlassian import Confluence
from bs4 import BeautifulSoup
from jinja2 import Template
from wrapt_timeout_decorator import *
from azure.ai.inference import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError
from azure.ai.inference.models import SystemMessage, UserMessage

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load and retrieve environment variables from .env file
load_dotenv()
confluence_api_token = os.getenv('CONFLUENCE_API_TOKEN')
azure_ai_key = os.getenv('AZURE_AI_KEY')


def load_config(config_path="config.toml"):
    """Load and return configuration from a TOML file as an immutable mapping."""
    with open(config_path, "r", encoding="utf-8") as file:
        return MappingProxyType(toml.load(file))  # Immutable config


def get_config():
    """Extract and return relevant configuration values."""
    config = load_config()
    return {
        "webapp_title": config["webapp"]["title"],
        "webapp_host": config["webapp"]["host"],
        "webapp_port": config["webapp"]["port"],
        "confluence_url": config["confluence"]["wiki_url"],
        "confluence_username": config["confluence"]["username"],
        "azure_ai_endpoint": config["azure-ai"]["endpoint"],
        "deployment_name": config["azure-ai"]["model"]["deployment_name"],
        "max_tokens": config["azure-ai"]["model"]["max_tokens"],
        "temperature": config["azure-ai"]["model"]["temperature"],
        "top_p": config["azure-ai"]["model"]["top_p"],
        "token_cost_batch": config["azure-ai"]["model"]["token_cost_batch"],
        "input_token_cost": config["azure-ai"]["model"]["input_token_cost"],
        "output_token_cost": config["azure-ai"]["model"]["output_token_cost"],
        "system_prompt": config["prompts"]["system_page_rating"],
        "user_prompt": config["prompts"]["user_page_rating"],
        "cache_db": config["cache"]["database"],
        "cache_ttl": config["cache"]["ttl_seconds"]
    }

# Retrieve config
config = get_config()

# Ensure cache_db path exists
os.makedirs(os.path.dirname(f"cache/{config['cache_db']}"), exist_ok=True)

# Create Confluence instance
confluence = Confluence(
        url=config["confluence_url"],
        username=config["confluence_username"],
        password=confluence_api_token
    )


def get_confluence_content(page_url: str):
    """Fetches and extracts Confluence page content as HTML."""
    page_id = page_url.split('/')[-2]
    page = confluence.get_page_by_id(page_id, expand="body.storage")
    
    if page:
        return page_id, page["body"]["storage"]["value"]
    return page_id, None


def extract_main_content(html_content: str):
    """Extracts Confluence HTML and do a little clean up."""
    soup = BeautifulSoup(html_content, "html.parser")
    # Remove Confluence-specific metadata, scripts, and navigation elements
    for tag in soup(["script", "style", "meta", "header", "footer", "nav"]):
        tag.decompose()

    return str(soup)  # Return cleaned HTML as a string


def init_db():
    """Create a table for caching ratings if it doesn't exist."""
    with sqlite3.connect(config["cache_db"]) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cc_page_ratings (
                page_id TEXT PRIMARY KEY,
                content_hash TEXT,
                rating_response TEXT,
                timestamp INTEGER
            )
        """)
        conn.commit()


def compute_hash(content: str) -> str:
    """Generate a hash of the content to detect changes."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_cached_rating(page_id: str, content: str):
    """Retrieve the cached rating if the page hasn't changed and TTL is valid."""
    with sqlite3.connect(config["cache_db"]) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT content_hash, rating_response, timestamp FROM cc_page_ratings WHERE page_id = ?", (page_id,))
        result = cursor.fetchone()

        if result:
            cached_hash, rating_response, timestamp = result
            if cached_hash == compute_hash(content) and (time.time() - timestamp) < config["cache_ttl"]:
                logger.info(f"Using cached rating for page_id {page_id}")
                return json.loads(rating_response)

        return None  # No valid cache found


def update_cache(page_id: str, content: str, rating_response: dict):
    """Update or insert a new rating into the cache."""
    with sqlite3.connect(config["cache_db"]) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO cc_page_ratings (page_id, content_hash, rating_response, timestamp)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                content_hash = excluded.content_hash,
                rating_response = excluded.rating_response,
                timestamp = excluded.timestamp
        """, (page_id, compute_hash(content), json.dumps(rating_response), int(time.time())))
        conn.commit()


def cleanup_cache():
    """Remove expired cache entries."""
    with sqlite3.connect(config["cache_db"]) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM page_ratings WHERE timestamp < ?", (int(time.time()) - config["cache_ttl"],))
        conn.commit()


@timeout(30, use_signals=False)
def rate_page(wiki_content: str, model: str):
    """Consruct a prompt with the page content and use a model to rate the page content."""
    try:
        # Initialize Azure client
        client = ChatCompletionsClient(
            endpoint=config["azure_ai_endpoint"],
            credential=AzureKeyCredential(azure_ai_key),
        )

        # Construct user prompt
        user_prompt_template = Template(config["user_prompt"])
        user_prompt = user_prompt_template.render(wiki_content=wiki_content)
        
        # Call the LLM
        try:
            response = client.complete(
                model=model,
                messages=[ 
                    SystemMessage(config["system_prompt"]),
                    UserMessage(user_prompt)
                ],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
                top_p=config["top_p"]
            )
            # Extract and return the ratings, recommendations, and token usage
            result = response.choices[0].message.content.strip()
            prompt_tokens = response.usage.prompt_tokens
            completion_tokens = response.usage.completion_tokens
            return result, prompt_tokens, completion_tokens
        
        except requests.Timeout:
            logger.error("Azure API request timed out.")
            return "The request timed out. Please try again later.", 0, 0
        
        except AzureError as e:
            logger.error("Azure API error occurred: %s", e)
            return "Sorry, we're having trouble processing your request. Please try again later.", 0, 0

        except Exception as e:
            logger.error("Unexpected error occurred: %s", e)
            return "An unexpected error occurred. Please try again later.", 0, 0

    except TimeoutError:
        logger.error("Function execution timed out.")
        return "Processing timed out. Please try again later.", 0, 0
         
    except Exception as e:
        logger.error("Failed to rate the document: %s", e)
        return "We encountered an issue while processing the document. Please try again later.", 0, 0


def get_page_rating(wiki_url: str, model: str):
    """Fetches and rates a Confluence document, handling errors gracefully."""
    try:
        page_id, html_content = get_confluence_content(wiki_url)
        if not (page_id and html_content):  
            logging.error(f"Failed to retrieve content from {wiki_url}")
            return {"error": "Failed to retrieve Confluence content."}
        
        try:
            formatted_text = extract_main_content(html_content)
        except Exception as e:
            logging.exception("Error extracting content from Confluence HTML.")
            return {"error": "Failed to extract content from Confluence.", "details": str(e)}
        
        
        try:
            # Check cache
            init_db()            
            cached_rating = get_cached_rating(page_id, formatted_text)
            if cached_rating:
                rating = {
                    "answer": cached_rating["answer"],
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "input_tokens_cost": 0,
                    "output_tokens_cost": 0
                }
                return rating
                
            
            print("Fetching fresh rating from LLM...")
            try:
                result, prompt_tokens, completion_tokens = rate_page(formatted_text, model)
                input_tokens_cost = f"{(( prompt_tokens / config['token_cost_batch']) * config['input_token_cost']):.4f}"
                output_tokens_cost = f"{(( completion_tokens / config['token_cost_batch']) * config['output_token_cost']):.4f}"
                rating = {
                    "answer": result,
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "input_tokens_cost": input_tokens_cost,
                    "output_tokens_cost": output_tokens_cost
                }

                # Update cache after successful rating
                update_cache(page_id, formatted_text, rating)
                return rating
            
            except Exception as e:
                logging.exception("Error while rating the document.")
                return {"error": "Failed to rate the document.", "details": str(e)}

        except sqlite3.DatabaseError as db_err:
            print(f"Database error: {db_err}")
        except json.JSONDecodeError as json_err:
            print(f"JSON processing error: {json_err}")
        except Exception as e:
            print(f"Unexpected error: {e}")            
    
    except Exception as e:
        logging.exception("Unexpected error in get_document_rating function.")
        return {"error": "An unexpected error occurred.", "details": str(e)}