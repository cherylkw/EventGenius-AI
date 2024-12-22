import streamlit as st
import requests
import openai
import json
import os
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
import pandas as pd
import uuid

# Clear session state for fresh start
if "reset_session" not in st.session_state:
    st.session_state.clear()
    st.session_state["reset_session"] = True

# Ensure workflow_log is initialized
if "workflow_log" not in st.session_state:
    st.session_state["workflow_log"] = []
if "feedback_selection" not in st.session_state:
    st.session_state["feedback_selection"] = {}

# Load environment variables
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
TICKETMASTER_API_KEY = os.getenv("EVENTS_API_KEY")

# Custom adapter to convert datetime to string
def adapt_datetime(dt):
    return dt.isoformat()

# Custom converter to convert string back to datetime
def convert_datetime(s):
    return datetime.fromisoformat(s.decode("utf-8"))

# Register the adapter and converter
sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)

# Database connection
conn = sqlite3.connect(
    'user_data.db', 
    detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, 
    check_same_thread=False
)
cursor = conn.cursor()

# Create tables if not exist
cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_preferences (
        user_id TEXT PRIMARY KEY,
        preferences TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS query_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        query TEXT,
        response TEXT,
        feedback TEXT DEFAULT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")

# Ensure workflow_log table exists
cursor.execute("""
    CREATE TABLE IF NOT EXISTS workflow_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        step TEXT,
        output TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")

conn.commit()

# Generate and store UUID for user identification
def get_user_id():
    if "user_id" not in st.session_state:
        st.session_state["user_id"] = str(uuid.uuid4())  # Generate a unique UUID
    return st.session_state["user_id"]

# Helper functions for database interactions
def save_preferences(user_id, new_preference):
    try:
        # Fetch existing preferences
        current_preferences = get_preferences(user_id)
        if current_preferences is None:
            current_preferences = []  # Initialize as an empty list if no preferences exist

        # Check if the new preference already exists to avoid duplicates
        if new_preference not in current_preferences:
            current_preferences.append(new_preference)  # Add the new preference to the list

        # Save updated preferences to the database
        cursor.execute("""
            INSERT OR REPLACE INTO user_preferences (user_id, preferences, updated_at)
            VALUES (?, ?, ?)
        """, (user_id, json.dumps(current_preferences), datetime.now()))
        conn.commit()
    except sqlite3.Error as e:
        st.error(f"Error saving preferences: {e}")

def get_preferences(user_id):
    try:
        cursor.execute("SELECT preferences FROM user_preferences WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, json.JSONDecodeError) as e:
        st.error(f"Error retrieving preferences: {e}")
        return None

def log_query(user_id, query, response):
    try:
        cursor.execute("""
            INSERT INTO query_history (user_id, query, response, timestamp)
            VALUES (?, ?, ?, ?)
        """, (user_id, query, response, datetime.now()))
        conn.commit()
        return cursor.lastrowid  # Return the ID of the inserted row
    except sqlite3.Error as e:
        st.error(f"Error logging query: {e}")
        return None

def log_workflow(user_id, workflow_log):
    try:
        for log in workflow_log:
            cursor.execute("""
                INSERT INTO workflow_log (user_id, step, output)
                VALUES (?, ?, ?)
            """, (user_id, log['step'], json.dumps(log['output'])))
        conn.commit()
    except sqlite3.Error as e:
        st.error(f"Error logging workflow: {e}")

def get_query_history():
    try:
        cursor.execute("SELECT * FROM query_history")
        rows = cursor.fetchall()
        return rows
    except sqlite3.Error as e:
        st.error(f"Error fetching query history: {e}")
        return []

def get_workflow_logs():
    try:
        cursor.execute("SELECT * FROM workflow_log")
        rows = cursor.fetchall()
        return rows
    except sqlite3.Error as e:
        st.error(f"Error fetching workflow logs: {e}")
        return []

def delete_query_history():
    try:
        cursor.execute("DELETE FROM query_history")
        conn.commit()
    except sqlite3.Error as e:
        st.error(f"Error clearing query history: {e}")

# Manager to orchestrate sub-agents
def manager(input_prompt, user_id):
    workflow_log = []
    try:
        # Task 1: Extract keywords using GPT-3.5
        keywords = extract_keywords(input_prompt)
        workflow_log.append({"step": "Extract Keywords", "output": keywords})
        if "error" in keywords:
            return "Failed to extract keywords. Please refine your query.", workflow_log

        # Create a preference dictionary based on the extracted keywords
        new_preference = {
            "artist": keywords.get("keyword", ""),
            "location": keywords.get("city", ""),
            "timeframe": f"{keywords.get('startDateTime', '')} to {keywords.get('endDateTime', '')}"
        }
        save_preferences(user_id, new_preference)
        workflow_log.append({"step": "Save Preferences", "output": new_preference})

        # Task 2: Build query and fetch events
        events = fetch_events(keywords)
        workflow_log.append({"step": "Fetch Events", "output": events})
        if "error" in events:
            return events["error"], workflow_log

        # Task 3: Infer and generate response
        output_response = generate_response(events)
        workflow_log.append({"step": "Generate Response", "output": events})

        # Log query and workflow
        query_id = log_query(user_id, input_prompt, output_response)
        log_workflow(user_id, workflow_log)

        return output_response, workflow_log, query_id
    except Exception as e:
        workflow_log.append({"step": "Error", "output": str(e)})
        return f"An unexpected error occurred: {str(e)}", workflow_log, None

# Task 1: Extract keywords using GPT-3.5
def extract_keywords(input_prompt):
    messages = [
        {
            "role": "system",
            "content": "You are a music event assistant. Your task is to understand the user's input, extract relevant keywords for artists, locations, and timeframes, and generate a query for the Ticketmaster API. Ensure that the 'keyword' extracted from the user query matches the exact artist's name or band name provided by the user without including partial matches."
        },
        {
            "role": "user",
            "content": f"""
            Given the following user query, identify:
            1. Keywords (artists, genres, etc.).
            2. Location (city or region).
            3. Dates or timeframes.

            Construct a Ticketmaster API query in the following JSON format:
            {{
                "keyword": "<artist/genre>",
                "city": "<city>",
                "startDateTime": "<ISO 8601 start date>",
                "endDateTime": "<ISO 8601 end date>"
            }}

            User Query: "{input_prompt}"
            """
        }
    ]
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        max_tokens=100,
        temperature=0.1
    )
    try:
        return json.loads(response["choices"][0]["message"]["content"])
    except json.JSONDecodeError:
        return {"error": "Failed to parse keywords"}

# Task 2: Fetch events from Ticketmaster
def fetch_events(query):
    base_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    params = {
        "keyword": f'"{query.get("keyword", "")}"',  # Enclose in quotes for exact match
        "city": query.get("city", ""),
        "startDateTime": query.get("startDateTime", ""),
        "endDateTime": query.get("endDateTime", ""),
        "apikey": TICKETMASTER_API_KEY,
    }
    try:
        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        events = response.json()

        # Filter events to match exact artist name
        if "_embedded" in events:
            filtered_events = [
                event for event in events["_embedded"]["events"]
                if query.get("keyword", "").strip('"') in event.get("name", "")
            ]
            events["_embedded"]["events"] = filtered_events

        return events
    except requests.exceptions.RequestException as e:
        return {"error": f"API error: {e}"}

# Task 3: Generate response using GPT-3.5
def generate_response(events):
    if "error" in events:
        return events["error"]

    # Prepare a detailed prompt for GPT-3.5
    event_summaries = "\n".join([
        f"{i + 1}. Event Name: {event.get('name', 'Unknown Event')}\n"
        f"Venue: {event.get('_embedded', {}).get('venues', [{}])[0].get('name', 'Unknown Venue')} in {event.get('_embedded', {}).get('venues', [{}])[0].get('city', {}).get('name', 'Unknown City')}\n"
        f"Date and Time: {event.get('dates', {}).get('start', {}).get('localDate', 'Unknown Date')} at {event.get('dates', {}).get('start', {}).get('localTime', 'Unknown Time')}\n"
        f"Ticket Price: {f'${{{event.get('priceRanges', [{}])[0].get('min', 'N/A')}}} - ${{{event.get('priceRanges', [{}])[0].get('max', 'N/A')}}}' if event.get('priceRanges') else 'Price not specified'}\n"
        f"Event URL: {event.get('url', '#')}\n"
        f"Event Image: {event.get('images', [{}])[0].get('url', 'Image not available')}\n"
        for i, event in enumerate(events.get("_embedded", {}).get("events", []))
    ])

    prompt = f"""
    You are a helpful assistant providing detailed responses about music events. Based on the following event details, generate a friendly and engaging response to present these events to the user:
    
    Event Details:
    {event_summaries}
    
    If no events are found, provide a polite and helpful message encouraging the user to try a different query.
    """

    # Call GPT-3.5 for response generation
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Insert prompt here : Recommend relevant live music events based ...."
            },
            {"role": "user", "content": prompt}
        ],
        max_tokens=500,
        temperature=0.1
    )

    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return "Sorry, an error occurred while generating the response. Please try again later."

# Streamlit UI with Tabs for Main and Admin Pages
tabs = st.tabs(["Main Page", "Admin Page"])

with tabs[0]:
    st.markdown('<h2 class="title-font">Music Events Recommendation Assistant 🎵</h2>', unsafe_allow_html=True)

    user_id = get_user_id()
    st.write(f"Your unique user ID: {user_id}")  # Display the auto-generated user ID

    input_prompt = st.text_input("Enter your query (e.g., 'Find Kenny G concerts in California next January'):")
    if st.button("Search"):
        with st.spinner("Processing..."):
            result, workflow_log, query_id = manager(input_prompt, user_id)
            st.session_state["workflow_log"] = workflow_log  # Update workflow_log in session state immediately
            st.session_state["last_query"] = input_prompt  # Store the last query
            st.session_state["last_result"] = result  # Store the last result
            st.session_state["last_query_id"] = query_id  # Store the query ID for feedback updates
            st.markdown(result, unsafe_allow_html=True)

    # Display previous results and feedback section if search is not triggered
    if "last_query" in st.session_state and "last_result" in st.session_state:
        st.markdown(st.session_state["last_result"], unsafe_allow_html=True)

        # Feedback section
        if st.session_state["last_query"] not in st.session_state["feedback_selection"]:
            st.session_state["feedback_selection"][st.session_state["last_query"]] = "Like"  # Default value

        feedback = st.radio(
            "Was this result helpful?",
            options=["Like", "Dislike"],
            horizontal=True,
            key=f"feedback_{st.session_state['last_query']}",
            index=["Like", "Dislike"].index(st.session_state["feedback_selection"][st.session_state["last_query"]]),
        )

        if st.button("Submit Feedback"):
            feedback = st.session_state["feedback_selection"][st.session_state["last_query"]]
            try:
                cursor.execute("""
                    UPDATE query_history
                    SET feedback = ?, timestamp = ?
                    WHERE id = ?
                """, (feedback, datetime.now(), st.session_state["last_query_id"]))
                conn.commit()
                st.success("Thank you for your feedback!")
            except sqlite3.Error as e:
                st.error(f"Error updating feedback: {e}")


    # Update sidebar with the current workflow log
    st.sidebar.markdown("## Workflow Logs")
    workflow_log = st.session_state["workflow_log"]
    for log in workflow_log:
        with st.sidebar.expander(f"{log['step']} - {'Success' if 'error' not in log['output'] else 'Failed'}", expanded=True):
            st.sidebar.markdown(f"**Step:** {log['step']}")
            if isinstance(log['output'], dict):
                st.sidebar.json(log['output'])
            elif isinstance(log['output'], str) and log['step'] != 'Generate Response':
                st.sidebar.markdown(log['output'], unsafe_allow_html=True)

with tabs[1]:
    st.markdown("### User Preferences")
    cursor.execute("SELECT user_id, preferences, created_at, updated_at FROM user_preferences")
    preferences_data = cursor.fetchall()
    preferences_df = pd.DataFrame(preferences_data, columns=["User ID", "Preferences", "Created At", "Updated At"])
    st.dataframe(preferences_df)

    st.markdown("### Query History")
    query_history = get_query_history()
    query_history_df = pd.DataFrame(query_history, columns=["ID", "User ID", "Query", "Response", "Feedback", "Timestamp"])
    st.dataframe(query_history_df)

    st.markdown("### Workflow Logs")
    workflow_logs = get_workflow_logs()
    workflow_logs_df = pd.DataFrame(workflow_logs, columns=["ID", "User ID", "Step", "Output", "Timestamp"])
    st.dataframe(workflow_logs_df)

    if st.button("Clear Query History"):
        delete_query_history()
        st.success("Query history cleared.")
