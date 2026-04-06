import asyncio
import asyncpg
import os

PG_HOST     = os.environ.get("PG_HOST", "34.93.217.19")
PG_DB       = os.environ.get("PG_DB", "domains")
PG_USER     = os.environ.get("PG_USER", "scraper")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Custarea@1")
PG_PORT     = int(os.environ.get("PG_PORT", "5432"))

async def delete_temp_errors():
    print(f"Connecting to Cloud SQL at {PG_HOST}...")
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, database=PG_DB,
        user=PG_USER, password=PG_PASSWORD
    )
    
    query = """
        SELECT COUNT(*)
        FROM domain_crawl_status
        WHERE error_msg IS NOT NULL 
          AND error_msg != ''
          AND error_msg NOT IN (
            'connection_failed_429',
            'connection_failed_403',
            'connection_failed_500',
            'connection_failed_200',
            'connection_failed_503',
            'connection_failed_404',
            'connection_failed_522',
            'connection_failed_502',
            'connection_failed_406',
            'connection_failed_408'
          )
          AND error_msg NOT LIKE 'A child process terminated abruptly%';
    """
    cnt = await conn.fetchval(query)
    print(f"Rows identified for retry (transient errors to be deleted): {cnt}")
    
    if cnt > 0:
        delete_query = """
            DELETE FROM domain_crawl_status
            WHERE error_msg IS NOT NULL 
              AND error_msg != ''
              AND error_msg NOT IN (
                'connection_failed_429',
                'connection_failed_403',
                'connection_failed_500',
                'connection_failed_200',
                'connection_failed_503',
                'connection_failed_404',
                'connection_failed_522',
                'connection_failed_502',
                'connection_failed_406',
                'connection_failed_408'
              )
              AND error_msg NOT LIKE 'A child process terminated abruptly%';
        """
        status = await conn.execute(delete_query)
        print(f"Deletion complete. Status: {status}")
        print("These domains will now be automatically picked up by the crawler on the next run.")
    else:
        print("No rows to delete.")
        
    await conn.close()

if __name__ == "__main__":
    import platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(delete_temp_errors())
