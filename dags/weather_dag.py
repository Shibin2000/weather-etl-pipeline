import logging
import os
import time
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd
import requests
from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator

log = logging.getLogger(__name__)

default_args = {
    "owner": "shibin",
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
}

API_KEY  = Variable.get("OPENWEATHER_API_KEY", default_var=os.getenv("OPENWEATHER_API_KEY", ""))
DB_PATH  = Variable.get("weather_db_path", default_var="/opt/airflow/data/weather_warehouse.db")
BASE_URL = "https://api.openweathermap.org/data/2.5/weather"

CITIES = [
    # houston first bc thats where i live, easier to sanity check the temps`n    "Houston,US", "Dallas,US", "Austin,US", "San Antonio,US", "New York,US",
    "Chicago,US", "Los Angeles,US", "Phoenix,US", "Seattle,US", "Miami,US",
]


def _fetch_with_retry(city: str, max_retries: int = 3) -> dict:
    """Fetch weather for one city with exponential backoff retry."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                BASE_URL,
                params={"q": city, "appid": API_KEY, "units": "imperial"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Attempt %d failed for %s: %s. Retrying in %ds",
                        attempt + 1, city, exc, wait)
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                log.error("All retries exhausted for %s", city)
    return {}


def extract_weather(**ctx):
    records = []
    for city in CITIES:
        data = _fetch_with_retry(city)
        if not data:
            log.warning("Skipping %s — no data returned", city)
            continue
        records.append({
            "city":           city.split(",")[0],
            "country":        data.get("sys", {}).get("country"),
            "temperature_f":  data["main"]["temp"],
            "feels_like_f":   data["main"]["feels_like"],
            "temp_min_f":     data["main"]["temp_min"],
            "temp_max_f":     data["main"]["temp_max"],
            "humidity_pct":   data["main"]["humidity"],
            "pressure_hpa":   data["main"]["pressure"],
            "wind_speed_mph": data["wind"]["speed"],
            "wind_deg":       data["wind"].get("deg"),
            "visibility_m":   data.get("visibility"),
            "description":    data["weather"][0]["description"],
            "weather_main":   data["weather"][0]["main"],
            "extracted_at":   datetime.now(timezone.utc).isoformat(),  # was utcnow(), deprecated in 3.12
        })
        log.info("Fetched: %s — %.1fF", city, data["main"]["temp"])

    raw_path = f"/tmp/weather_{ctx['run_id'].replace(':', '_')}_raw.parquet"
    pd.DataFrame(records).to_parquet(raw_path, index=False)
    ctx["ti"].xcom_push(key="raw_path", value=raw_path)
    log.info("Extracted %d cities", len(records))


def transform_weather(**ctx):
    raw_path = ctx["ti"].xcom_pull(key="raw_path")
    df = pd.read_parquet(raw_path)

    df["temp_c"] = ((df["temperature_f"] - 32) * 5 / 9).round(1)
    # Hot/Warm/Cool/Cold - made up the thresholds based on what feels right in houston`n    df["heat_index"] = df["heat_index"] if "heat_index" in df.columns else df["temperature_f"].apply(
        lambda t: "Hot" if t >= 90 else "Warm" if t >= 70
        else "Cool" if t >= 50 else "Cold"
    )
    df["extracted_date"] = pd.to_datetime(df["extracted_at"]).dt.date

    # took forever to figure out the backoff, finally works - turned out to be a bad api response
    # assertions catch this before it pollutes the db
    # data quality assertions before writing
    assert df["temperature_f"].between(-60, 150).all(), "Temperature out of plausible range"
    assert df["humidity_pct"].between(0, 100).all(), "Humidity out of range"
    assert df["city"].notnull().all(), "Null city detected"

    clean_path = raw_path.replace("raw", "clean")
    df.to_parquet(clean_path, index=False)
    ctx["ti"].xcom_push(key="clean_path", value=clean_path)
    log.info("Transform complete: %d rows", len(df))


def load_weather(**ctx):
    clean_path = ctx["ti"].xcom_pull(key="clean_path")
    df = pd.read_parquet(clean_path)

    conn = duckdb.connect(DB_PATH)
    # append-only — preserves history across daily runs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_readings (
            city VARCHAR, country VARCHAR,
            temperature_f DOUBLE, feels_like_f DOUBLE,
            temp_min_f DOUBLE, temp_max_f DOUBLE,
            humidity_pct INTEGER, pressure_hpa INTEGER,
            wind_speed_mph DOUBLE, wind_deg INTEGER,
            visibility_m INTEGER, description VARCHAR,
            weather_main VARCHAR, extracted_at VARCHAR,
            temp_c DOUBLE, heat_index VARCHAR, extracted_date DATE
        )
    """)
    conn.execute("INSERT INTO weather_readings SELECT * FROM df")
    total = conn.execute("SELECT COUNT(*) FROM weather_readings").fetchone()[0]
    log.info("weather_readings total rows after insert: %d", total)
    conn.close()


# runs after load - gives a quick sanity check in the logs`ndef analytical_queries(**ctx):
    conn = duckdb.connect(DB_PATH)

    result = conn.execute("""
        SELECT city,
               ROUND(temperature_f, 1) as temp_f,
               ROUND(temp_c, 1) as temp_c,
               humidity_pct, heat_index, description
        FROM weather_readings
        WHERE extracted_date = CURRENT_DATE
        ORDER BY temperature_f DESC
    """).df()
    log.info("=== Today's city rankings ===\n%s", result.to_string(index=False))

    hottest = conn.execute(
        "SELECT city FROM weather_readings WHERE extracted_date = CURRENT_DATE "
        "ORDER BY temperature_f DESC LIMIT 1"
    ).fetchone()
    coldest = conn.execute(
        "SELECT city FROM weather_readings WHERE extracted_date = CURRENT_DATE "
        "ORDER BY temperature_f ASC LIMIT 1"
    ).fetchone()
    if hottest:
        log.info("Hottest city today: %s", hottest[0])
    if coldest:
        log.info("Coldest city today: %s", coldest[0])
    conn.close()


with DAG(
    dag_id="weather_etl_pipeline",
    description="Daily weather ETL for 10 US cities via OpenWeatherMap API",
    default_args=default_args,
    schedule_interval="@daily",
    start_date=datetime(2026, 3, 1),
    catchup=False,
    tags=["weather", "api", "duckdb"],
) as dag:

    t1 = PythonOperator(task_id="extract_weather",    python_callable=extract_weather)
    t2 = PythonOperator(task_id="transform_weather",  python_callable=transform_weather)
    t3 = PythonOperator(task_id="load_weather",       python_callable=load_weather)
    t4 = PythonOperator(task_id="analytical_queries", python_callable=analytical_queries)

    t1 >> t2 >> t3 >> t4











