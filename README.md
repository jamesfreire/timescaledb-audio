# Soundscape Analyzer

Real-time environmental audio monitoring and analysis system with decisecond resolution storage in TimescaleDB.

## Overview

Soundscape analyzer is a Python-based system that captures audio from microphones, processes it to extract acoustic features (decibel levels and frequency bands), and stores the data in TimescaleDB for advanced time-series analysis. T

## Features

- 📊 **High-resolution audio monitoring** - Capture audio metrics at 0.1-second (decisecond) resolution
- 🔊 **Complete frequency analysis** - Extract and analyze 7 frequency bands (sub-bass to brilliance)
- 📈 **Efficient time-series storage** - Leverage TimescaleDB's hypertables and continuous aggregates
- 🔍 **Real-time visualization** - Monitor acoustic environments through Grafana dashboards
- 🏙️ **Spatial noise mapping** - Track and compare noise levels across different   locations

## Installation

### Prerequisites

- Python 3.8+
- PostgreSQL 12+ with TimescaleDB extension
- Grafana 9+ (for visualization)

### Python Dependencies

```bash
pip install numpy sounddevice psycopg2-binary scipy
```

### TimescaleDB Setup

1. Install TimescaleDB following the [official instructions](https://docs.timescale.com/install/latest/self-hosted/)

2. Create database and enable TimescaleDB extension:

```sql
CREATE DATABASE soundscape;
\c soundscape
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
```

3. Run the setup script to create the necessary tables:

```bash
python setup_db.py --db-host localhost --db-port 5432 --db-name soundscape --db-user postgres --db-password yourpassword
```

## Usage

### Basic Monitoring

Start the audio monitor with:

```bash
python audio_monitor.py \
  --db-host localhost \
  --db-port 5432 \
  --db-name soundscape \
  --db-user postgres \
  --db-password yourpassword \
  --location-id downtown-01
```

### Run as a Service

To run as a background service using systemd (Linux):

1. Create a service file at `/etc/systemd/system/audio-monitor.service`:

```ini
[Unit]
Description=Soundscape Audio Monitor
After=network.target postgresql.service

[Service]
Type=simple
User=youruser
ExecStart=/usr/bin/python3 /path/to/audio_monitor.py \
  --db-host localhost \
  --db-port 5432 \
  --db-name soundscape \
  --db-user postgres \
  --db-password yourpassword \
  --location-id downtown-01
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

2. Enable and start the service:

```bash
sudo systemctl enable audio-monitor
sudo systemctl start audio-monitor
```

## Core Components

### Python Audio Monitor (`audio_monitor.py`)

The main script that:
- Captures audio data from the microphone
- Processes it to extract acoustic features
- Stores data in TimescaleDB

```python
# Key functions:
calculate_decibel(audio_chunk, reference=1.0)  # Converts audio data to decibel scale
analyze_frequency(audio_data)                  # Extracts frequency bands from audio data 
```

### TimescaleDB Schema

```sql
-- Sensor metadata table
CREATE TABLE sound_sensors (
    sensor_id TEXT PRIMARY KEY,
    location_id TEXT NOT NULL,
    description TEXT,
    installation_date TIMESTAMPTZ DEFAULT NOW()
);

-- Raw metrics table as a hypertable
CREATE TABLE sound_metrics (
    time TIMESTAMPTZ NOT NULL,
    sensor_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    decibel_level FLOAT NOT NULL,
    frequency_bands JSONB NOT NULL,
    FOREIGN KEY (sensor_id) REFERENCES sound_sensors(sensor_id)
);

-- Convert to TimescaleDB hypertable
SELECT create_hypertable('sound_metrics', 'time');
```

### Continuous Aggregates

For efficient querying of historical data:

```sql
-- Create minute-level aggregates
CREATE MATERIALIZED VIEW sound_metrics_minute
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    sensor_id,
    location_id,
    AVG(decibel_level) AS avg_decibel,
    MAX(decibel_level) AS max_decibel,
    MIN(decibel_level) AS min_decibel,
    jsonb_build_object(
        'sub_bass', AVG((frequency_bands->>'sub_bass')::float),
        'bass', AVG((frequency_bands->>'bass')::float),
        'low_mid', AVG((frequency_bands->>'low_mid')::float),
        'mid', AVG((frequency_bands->>'mid')::float),
        'upper_mid', AVG((frequency_bands->>'upper_mid')::float),
        'presence', AVG((frequency_bands->>'presence')::float),
        'brilliance', AVG((frequency_bands->>'brilliance')::float)
    ) AS avg_frequency_bands
FROM sound_metrics
GROUP BY bucket, sensor_id, location_id;
```
