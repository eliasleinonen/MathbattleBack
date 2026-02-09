#!/usr/bin/env python3
"""
Script to seed the DailyChallenge collection in MongoDB
Run this once to populate the database with 1000 pre-generated daily challenges
"""

import asyncio
import random
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv

load_dotenv()

MONGODB_URL = os.getenv("DATABASE_URL", "mongodb://localhost:27017")
DATABASE_NAME = "derivative_duel"

# Import the challenge generation function from main.py
import sys
sys.path.insert(0, os.path.dirname(__file__))
from main import generate_question

async def seed_daily_challenges():
    """Seed 1000 daily challenges starting from Jan 1, 2024"""
    client = AsyncIOMotorClient(MONGODB_URL)
    db = client[DATABASE_NAME]
    daily_challenges_collection = db.DailyChallenge
    
    print(f"Connecting to MongoDB: {MONGODB_URL}")
    print(f"Database: {DATABASE_NAME}")
    print(f"Collection: DailyChallenge")
    
    # Clear existing data
    result = await daily_challenges_collection.delete_many({})
    print(f"Deleted {result.deleted_count} existing documents")
    
    # Generate challenges from Jan 1, 2024 for 1000 days
    start_date = datetime(2024, 1, 1, tzinfo=timezone.utc).date()
    challenges = []
    
    print("Generating 1000 daily challenges...")
    
    for day_offset in range(1000):
        challenge_date = start_date + timedelta(days=day_offset)
        date_str = challenge_date.isoformat()  # Format: YYYY-MM-DD
        
        # Use date hash for deterministic ELO
        date_hash = sum(ord(c) for c in date_str)
        elo_options = [1300, 1400, 1500, 1600]
        elo = elo_options[date_hash % len(elo_options)]
        
        # Generate question with deterministic seed
        old_state = random.getstate()
        random.seed(date_str)
        question = generate_question(elo)
        random.setstate(old_state)
        
        challenge = {
            "date": date_str,
            "expression": question["expression"],
            "derivative": question["derivative"],
            "answer": question["answer"],
            "difficulty": question.get("difficulty", 2),
            "elo": elo,
            "createdAt": datetime.now(timezone.utc)
        }
        
        challenges.append(challenge)
        
        if (day_offset + 1) % 100 == 0:
            print(f"  Generated {day_offset + 1} challenges...")
    
    # Insert in batches of 100
    print("Inserting challenges into MongoDB...")
    for i in range(0, len(challenges), 100):
        batch = challenges[i:i+100]
        result = await daily_challenges_collection.insert_many(batch)
        print(f"  Inserted batch {i//100 + 1}/10: {len(result.inserted_ids)} documents")
    
    # Verify insertion
    count = await daily_challenges_collection.count_documents({})
    print(f"\n✅ Successfully seeded {count} daily challenges!")
    
    # Show sample documents
    sample = await daily_challenges_collection.find_one()
    if sample:
        print(f"\nSample document:")
        print(f"  date: {sample.get('date')}")
        print(f"  expression: {sample.get('expression')}")
        print(f"  derivative: {sample.get('derivative')}")
        print(f"  answer: {sample.get('answer')}")
    
    # Show documents for 2025-12-10
    dec_10 = await daily_challenges_collection.find_one({"date": "2025-12-10"})
    if dec_10:
        print(f"\n2025-12-10 document found:")
        print(f"  expression: {dec_10.get('expression')}")
        print(f"  derivative: {dec_10.get('derivative')}")
        print(f"  answer: {dec_10.get('answer')}")
    else:
        print(f"\n⚠️  No document found for 2025-12-10")
        # Check what dates are around that
        all_dates = await daily_challenges_collection.find({}, {"date": 1}).to_list(None)
        dates = sorted([d.get('date') for d in all_dates])
        if dates:
            print(f"  Available dates range from {dates[0]} to {dates[-1]}")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(seed_daily_challenges())
