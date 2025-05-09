import numpy as np
import sounddevice as sd
import psycopg2
import json
import time
import logging
import argparse
import signal
import sys
from scipy.fft import rfft, rfftfreq
import uuid
from datetime import datetime
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("audio_monitor.log"), logging.StreamHandler()]
)
logger = logging.getLogger("AudioMonitor")

# Generate a unique identifier for this sensor
SENSOR_ID = str(uuid.uuid4())[:8]

class AudioMonitor:
    def __init__(self, db_host, db_port, db_name, db_user, db_password, 
                 location_id, sample_rate=44100, block_duration=0.1):
        """
        Initialize the audio monitor
        
        Args:
            db_*: Database connection parameters
            location_id: Identifier for this monitoring location
            sample_rate: Audio sample rate in Hz
            block_duration: Duration of each audio block in seconds
        """
        self.db_params = {
            "host": db_host,
            "port": db_port,
            "dbname": db_name,
            "user": db_user,
            "password": db_password
        }
        self.location_id = location_id
        self.sample_rate = sample_rate
        self.block_duration = block_duration
        self.block_size = int(sample_rate * block_duration)
        
        self.running = False
        self.stream = None
        self.conn = None
        self.cursor = None
        
        # Batch processing
        self.batch_buffer = []
        self.batch_lock = threading.Lock()
        self.batch_size = 10  # Start with smaller batches for direct insertion
        self.flush_interval = 1.0  # Flush every second
        self.flush_timer = None
        
    def start(self):
        """Start audio monitoring and database insertion"""
        try:
            # Connect to database
            self.conn = psycopg2.connect(**self.db_params)
            self.conn.autocommit = False
            self.cursor = self.conn.cursor()
            
            # Register sensor if needed
            self.register_sensor()
            
            logger.info(f"Connected to TimescaleDB at {self.db_params['host']}:{self.db_params['port']}")
            logger.info(f"Starting audio monitoring at location {self.location_id} with sensor {SENSOR_ID}")
            
            # Set up signal handling for graceful shutdown
            signal.signal(signal.SIGINT, self.handle_signal)
            signal.signal(signal.SIGTERM, self.handle_signal)
            
            # Start flush timer
            self.schedule_flush()
            
            # Start audio stream
            self.running = True
            self.stream = sd.InputStream(
                callback=self.audio_callback,
                channels=1,
                samplerate=self.sample_rate,
                blocksize=self.block_size
            )
            self.stream.start()
            
            # Keep main thread alive
            logger.info("Audio monitoring started. Press Ctrl+C to stop.")
            while self.running:
                time.sleep(0.5)
                
        except Exception as e:
            logger.error(f"Error in audio monitoring: {e}")
            self.stop()
            
    def stop(self):
        """Stop monitoring and clean up resources"""
        self.running = False
        
        # Cancel timer if running
        if self.flush_timer is not None:
            self.flush_timer.cancel()
            self.flush_timer = None
        
        # Flush any remaining records
        self.flush_batch()
        
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
            
        if self.cursor:
            self.cursor.close()
            self.cursor = None
            
        if self.conn:
            self.conn.close()
            self.conn = None
            
        logger.info("Audio monitoring stopped")
        
    def handle_signal(self, sig, frame):
        """Handle termination signals"""
        logger.info(f"Received signal {sig}, shutting down...")
        self.stop()
        sys.exit(0)
    
    def schedule_flush(self):
        """Schedule the next batch flush"""
        if not self.running:
            return
            
        self.flush_timer = threading.Timer(self.flush_interval, self.timed_flush)
        self.flush_timer.daemon = True
        self.flush_timer.start()
        
    def timed_flush(self):
        """Flush the batch on timer"""
        self.flush_batch()
        self.schedule_flush()
        
    def audio_callback(self, indata, frames, time_info, status):
        """Process audio data and add to batch"""
        if status:
            logger.warning(f"Audio status: {status}")
            
        try:
            # Extract audio data from first channel
            audio_data = indata[:, 0].copy()
            
            # Calculate metrics
            db_level = self.calculate_decibel(audio_data)
            frequency_bands = self.analyze_frequency(audio_data)
            
            # Create record
            timestamp = datetime.now().isoformat()
            record = {
                "timestamp": timestamp,
                "sensor_id": SENSOR_ID,
                "location_id": self.location_id,
                "decibel_level": float(db_level),
                "frequency_bands": frequency_bands
            }
            
            # Add to batch
            with self.batch_lock:
                self.batch_buffer.append(record)
                
                # If batch is full, flush it
                if len(self.batch_buffer) >= self.batch_size:
                    # Flush in a separate thread to avoid blocking audio processing
                    threading.Thread(target=self.flush_batch).start()
            
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
    
    def calculate_decibel(self, audio_chunk, reference=1.0):
        """Calculate decibel level from audio chunk"""
        # Convert to float and normalize
        audio_data = audio_chunk.astype(np.float32) / 32768.0
        
        # Root mean square (RMS) value
        rms = np.sqrt(np.mean(np.square(audio_data)))
        
        # Convert to decibel scale (dB)
        if rms > 0:
            db = 20 * np.log10(rms / reference)
        else:
            db = -96  # Approximately silence
            
        return db
    
    def analyze_frequency(self, audio_data):
        """Analyze frequency components of audio data"""
        # Apply window function to reduce spectral leakage
        windowed_data = audio_data * np.hanning(len(audio_data))
        
        # Perform FFT
        fft_data = np.abs(rfft(windowed_data))
        
        # Get frequency bins
        freqs = rfftfreq(len(audio_data), 1/self.sample_rate)
        
        # Group frequencies into bands
        bands = {}
        bands["sub_bass"] = float(np.mean(fft_data[(freqs >= 20) & (freqs < 60)]))
        bands["bass"] = float(np.mean(fft_data[(freqs >= 60) & (freqs < 250)]))
        bands["low_mid"] = float(np.mean(fft_data[(freqs >= 250) & (freqs < 500)]))
        bands["mid"] = float(np.mean(fft_data[(freqs >= 500) & (freqs < 2000)]))
        bands["upper_mid"] = float(np.mean(fft_data[(freqs >= 2000) & (freqs < 4000)]))
        bands["presence"] = float(np.mean(fft_data[(freqs >= 4000) & (freqs < 6000)]))
        bands["brilliance"] = float(np.mean(fft_data[(freqs >= 6000) & (freqs < 20000)]))
        
        return bands
    
    def register_sensor(self):
        """Register this sensor in the database if it doesn't exist"""
        try:
            # Check if sensor exists
            self.cursor.execute(
                "SELECT 1 FROM sound_sensors WHERE sensor_id = %s",
                (SENSOR_ID,)
            )
            
            if not self.cursor.fetchone():
                # Create sensor record
                self.cursor.execute(
                    """
                    INSERT INTO sound_sensors (sensor_id, location_id, description)
                    VALUES (%s, %s, %s)
                    """,
                    (SENSOR_ID, self.location_id, f"Sensor at {self.location_id}")
                )
                self.conn.commit()
                logger.info(f"Registered new sensor {SENSOR_ID} for location {self.location_id}")
            else:
                logger.info(f"Sensor {SENSOR_ID} already registered")
                
        except Exception as e:
            logger.error(f"Error registering sensor: {e}")
            self.conn.rollback()
            raise
            
    def flush_batch(self):
        """Flush the current batch to TimescaleDB"""
        with self.batch_lock:
            # Skip if batch is empty
            if not self.batch_buffer:
                return
                
            batch_to_flush = self.batch_buffer
            self.batch_buffer = []
            
        try:
            # Build values clause and parameters
            values_parts = []
            params = []
            
            for record in batch_to_flush:
                values_parts.append("(%s, %s, %s, %s, %s::jsonb)")
                params.extend([
                    record["timestamp"],
                    record["sensor_id"],
                    record["location_id"],
                    record["decibel_level"],
                    json.dumps(record["frequency_bands"])
                ])
                
            # Construct and execute query
            sql = f"""
            INSERT INTO sound_metrics (time, sensor_id, location_id, decibel_level, frequency_bands)
            VALUES {", ".join(values_parts)}
            """
            
            self.cursor.execute(sql, params)
            self.conn.commit()
            
            logger.info(f"Inserted batch of {len(batch_to_flush)} records")
            
        except Exception as e:
            logger.error(f"Error flushing batch: {e}")
            self.conn.rollback()

def setup_database(db_params):
    """Set up the database schema if it doesn't exist"""
    conn = None
    try:
        # Connect to database
        conn = psycopg2.connect(**db_params)
        conn.autocommit = True
        cursor = conn.cursor()
        
        # Check if TimescaleDB extension is installed
        cursor.execute("SELECT installed_version FROM pg_available_extensions WHERE name = 'timescaledb'")
        if cursor.fetchone() is None:
            logger.error("TimescaleDB extension is not available")
            return False
            
        # Create extension if not exists
        cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
        
        # Create tables
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sound_sensors (
            sensor_id TEXT PRIMARY KEY,
            location_id TEXT NOT NULL,
            description TEXT,
            installation_date TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sound_metrics (
            time TIMESTAMPTZ NOT NULL,
            sensor_id TEXT NOT NULL,
            location_id TEXT NOT NULL,
            decibel_level FLOAT NOT NULL,
            frequency_bands JSONB NOT NULL,
            FOREIGN KEY (sensor_id) REFERENCES sound_sensors(sensor_id)
        )
        """)
        
        # Check if table is already a hypertable
        cursor.execute("""
        SELECT * FROM timescaledb_information.hypertables 
        WHERE hypertable_name = 'sound_metrics'
        """)
        
        if cursor.fetchone() is None:
            # Create hypertable
            cursor.execute("""
            SELECT create_hypertable('sound_metrics', 'time', 
                                    chunk_time_interval => INTERVAL '1 hour',
                                    if_not_exists => TRUE)
            """)
            
        logger.info("Database schema setup complete")
        return True
        
    except Exception as e:
        logger.error(f"Error setting up database: {e}")
        return False
        
    finally:
        if conn:
            conn.close()

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Audio monitor for Urban Soundscape Analyzer")
    parser.add_argument("--db-host", default="localhost", help="Database host")
    parser.add_argument("--db-port", type=int, default=5432, help="Database port")
    parser.add_argument("--db-name", required=True, help="Database name")
    parser.add_argument("--db-user", required=True, help="Database user")
    parser.add_argument("--db-password", required=True, help="Database password")
    parser.add_argument("--location-id", required=True, help="Location identifier")
    parser.add_argument("--sample-rate", type=int, default=44100, help="Audio sample rate")
    parser.add_argument("--block-duration", type=float, default=0.1, help="Audio block duration in seconds")
    
    args = parser.parse_args()
    
    # Database connection parameters
    db_params = {
        "host": args.db_host,
        "port": args.db_port,
        "dbname": args.db_name,
        "user": args.db_user,
        "password": args.db_password
    }
    
    # Set up database schema
    if not setup_database(db_params):
        logger.error("Failed to set up database schema. Exiting.")
        sys.exit(1)
    
    # Create and start monitor
    monitor = AudioMonitor(
        args.db_host,
        args.db_port,
        args.db_name,
        args.db_user,
        args.db_password,
        args.location_id,
        args.sample_rate,
        args.block_duration
    )
    
    monitor.start()

if __name__ == "__main__":
    main()
