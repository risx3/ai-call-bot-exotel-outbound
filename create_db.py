import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# Database connection parameters
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

def create_crm_ai_db_table():
    """Create the crm-ai-db table with sid as primary key and Completed column."""
    
    try:
        # Connect to the database
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        
        cursor = conn.cursor()
        
        # Create table with sid as primary key and Completed with default value False
        create_table_query = """
        CREATE TABLE IF NOT EXISTS "crm-ai-db" (
            sid VARCHAR(255) PRIMARY KEY,
            "Completed" BOOLEAN DEFAULT FALSE
        );
        """
        
        cursor.execute(create_table_query)
        conn.commit()
        
        print("✅ Table 'crm-ai-db' created successfully!")
        
        cursor.close()
        conn.close()
        
    except psycopg2.Error as e:
        print(f"❌ Database error: {e}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    create_crm_ai_db_table()
