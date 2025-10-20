from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import yfinance as yf
import asyncio
from telegram import Bot
from telegram.error import TelegramError

load_dotenv()

# MongoDB connection
mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'bist_alerts_db')]

# Telegram Bot
telegram_bot = None
if os.environ.get('TELEGRAM_BOT_TOKEN'):
    telegram_bot = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])

# Create the main app
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Define Models
class StockSymbol(BaseModel):
    symbol: str
    name: str
    current_price: Optional[float] = None
    change_percent: Optional[float] = None

class AlertCreate(BaseModel):
    symbol: str
    alert_type: str
    value: float
    chat_id: str

class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    alert_type: str
    value: float
    chat_id: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_at: Optional[datetime] = None

# BIST symbols
BIST_SYMBOLS = [
    {"symbol": "XU100.IS", "name": "BIST 100"},
    {"symbol": "XU030.IS", "name": "BIST 30"},
    {"symbol": "THYAO.IS", "name": "Türk Hava Yolları"},
    {"symbol": "GARAN.IS", "name": "Garanti BBVA"},
    {"symbol": "ISCTR.IS", "name": "İş Bankası (C)"},
    {"symbol": "AKBNK.IS", "name": "Akbank"},
    {"symbol": "EREGL.IS", "name": "Ereğli Demir Çelik"},
    {"symbol": "TUPRS.IS", "name": "Tüpraş"},
    {"symbol": "SISE.IS", "name": "Şişe Cam"},
    {"symbol": "PETKM.IS", "name": "Petkim"},
    {"symbol": "KCHOL.IS", "name": "Koç Holding"},
    {"symbol": "SAHOL.IS", "name": "Sabancı Holding"},
    {"symbol": "BIMAS.IS", "name": "BIM"},
    {"symbol": "ASELS.IS", "name": "Aselsan"},
    {"symbol": "KOZAL.IS", "name": "Koza Altın"},
]

@api_router.get("/")
async def root():
    return {"message": "BIST Alert System", "status": "running"}

@api_router.get("/symbols", response_model=List[StockSymbol])
async def get_symbols():
    return BIST_SYMBOLS

@api_router.get("/price/{symbol}")
async def get_price(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d")
        
        if hist.empty:
            raise HTTPException(status_code=404, detail="Symbol not found")
        
        current_price = float(hist['Close'].iloc[-1])
        
        hist_2d = ticker.history(period="2d")
        if len(hist_2d) > 1:
            prev_close = float(hist_2d['Close'].iloc[-2])
            change_percent = ((current_price - prev_close) / prev_close) * 100
        else:
            change_percent = 0.0
        
        return {
            "symbol": symbol,
            "price": round(current_price, 2),
            "change_percent": round(change_percent, 2),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@api_router.post("/alerts", response_model=Alert)
async def create_alert(alert_input: AlertCreate):
    alert = Alert(**alert_input.model_dump())
    
    doc = alert.model_dump()
    doc['created_at'] = doc['created_at'].isoformat()
    if doc.get('triggered_at'):
        doc['triggered_at'] = doc['triggered_at'].isoformat()
    
    await db.alerts.insert_one(doc)
    return alert

@api_router.get("/alerts", response_model=List[Alert])
async def get_alerts(chat_id: Optional[str] = None, active_only: bool = True):
    query = {}
    if chat_id:
        query['chat_id'] = chat_id
    if active_only:
        query['is_active'] = True
    
    alerts = await db.alerts.find(query, {"_id": 0}).to_list(1000)
    
    for alert in alerts:
        if isinstance(alert.get('created_at'), str):
            alert['created_at'] = datetime.fromisoformat(alert['created_at'])
        if alert.get('triggered_at') and isinstance(alert['triggered_at'], str):
            alert['triggered_at'] = datetime.fromisoformat(alert['triggered_at'])
    
    return alerts

@api_router.delete("/alerts/{alert_id}")
async def delete_alert(alert_id: str):
    result = await db.alerts.delete_one({"id": alert_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"message": "Alert deleted"}

@api_router.post("/test-telegram")
async def test_telegram(chat_id: str):
    if not telegram_bot:
        raise HTTPException(status_code=400, detail="Telegram not configured")
    
    try:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="✅ Telegram bağlantısı başarılı! BIST bildirimleriniz bu sohbete gelecek."
        )
        return {"message": "Test message sent"}
    except TelegramError as e:
        raise HTTPException(status_code=500, detail=f"Telegram error: {str(e)}")

async def check_alerts_background():
    while True:
        try:
            if not telegram_bot:
                await asyncio.sleep(30)
                continue
            
            alerts = await db.alerts.find({"is_active": True}, {"_id": 0}).to_list(1000)
            
            for alert in alerts:
                try:
                    symbol = alert['symbol']
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period="2d")
                    
                    if hist.empty or len(hist) < 1:
                        continue
                    
                    current_price = float(hist['Close'].iloc[-1])
                    change_percent = 0.0
                    
                    if len(hist) > 1:
                        prev_close = float(hist['Close'].iloc[-2])
                        change_percent = ((current_price - prev_close) / prev_close) * 100
                    
                    triggered = False
                    message = ""
                    
                    if alert['alert_type'] == 'price_above' and current_price >= alert['value']:
                        triggered = True
                        message = f"🚀 {symbol}\n\nHedef fiyat aşıldı!\n💰 Fiyat: {current_price:.2f} TL\n🎯 Hedef: {alert['value']:.2f} TL\n📈 Değişim: {change_percent:+.2f}%"
                    
                    elif alert['alert_type'] == 'price_below' and current_price <= alert['value']:
                        triggered = True
                        message = f"⚠️ {symbol}\n\nHedef fiyatın altına düştü!\n💰 Fiyat: {current_price:.2f} TL\n🎯 Hedef: {alert['value']:.2f} TL\n📉 Değişim: {change_percent:+.2f}%"
                    
                    elif alert['alert_type'] == 'percent_up' and change_percent >= alert['value']:
                        triggered = True
                        message = f"📈 {symbol}\n\nYüzde artış hedefi!\n💰 Fiyat: {current_price:.2f} TL\n📊 Değişim: {change_percent:+.2f}%\n🎯 Hedef: {alert['value']:+.2f}%"
                    
                    elif alert['alert_type'] == 'percent_down' and change_percent <= -alert['value']:
                        triggered = True
                        message = f"📉 {symbol}\n\nYüzde düşüş hedefi!\n💰 Fiyat: {current_price:.2f} TL\n📊 Değişim: {change_percent:+.2f}%\n🎯 Hedef: -{alert['value']:.2f}%"
                    
                    if triggered:
                        await telegram_bot.send_message(
                            chat_id=alert['chat_id'],
                            text=message
                        )
                        
                        await db.alerts.update_one(
                            {"id": alert['id']},
                            {
                                "$set": {
                                    "is_active": False,
                                    "triggered_at": datetime.now(timezone.utc).isoformat()
                                }
                            }
                        )
                        
                        logging.info(f"Alert triggered: {symbol}")
                
                except Exception as e:
                    logging.error(f"Error checking alert: {str(e)}")
                    continue
            
            await asyncio.sleep(30)
        
        except Exception as e:
            logging.error(f"Background task error: {str(e)}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(check_alerts_background())
    logging.info("Alert checking started")

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
