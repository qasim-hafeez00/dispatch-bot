import asyncio
from cortexbot.core.queue_manager import queue_manager

async def main():
    print("Starting background worker...")
    await queue_manager.process_tasks()

if __name__ == "__main__":
    asyncio.run(main())
