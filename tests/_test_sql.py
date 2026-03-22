import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()

async def test():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL'), statement_cache_size=0)
    try:
        sql = (
            'INSERT INTO items '
            '("Item_Name", "Description", price, "Available_Status", "Category", '
            '"Subcategory", "Cuisine", "Spice_Level", "Prep_Time_Min", "Image_url", '
            'is_veg, tags, sort_order, dine_in_available, takeaway_available, '
            'delivery_available, restaurant_id, branch_id, user_id) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19) '
            'RETURNING *'
        )
        row = await conn.fetchrow(
            sql,
            'Test Item', 'Test desc', 100.0, True, 'Cat', 'SubCat', 'Indian',
            'Medium', 15, '', True, ['tag1'], 1, True, True, True,
            'cc0d821d-cc05-4b1e-8064-541e781d406f', None,
            'cc0d821d-cc05-4b1e-8064-541e781d406f',
        )
        print('SUCCESS:', dict(row))
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}')
    await conn.close()

asyncio.run(test())
