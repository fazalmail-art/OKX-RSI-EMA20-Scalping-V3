import os, time, json, hmac, base64, hashlib, threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone
from urllib.parse import urlencode
import requests
from flask import Flask, jsonify
from dotenv import load_dotenv
load_dotenv()

BASE_URL=os.getenv('OKX_BASE_URL','https://www.okx.com').rstrip('/')
API_KEY=os.getenv('OKX_API_KEY',''); SECRET_KEY=os.getenv('OKX_SECRET_KEY',''); PASSPHRASE=os.getenv('OKX_PASSPHRASE','')
DEMO=os.getenv('OKX_DEMO','true').lower()=='true'; AUTO_TRADE=os.getenv('AUTO_TRADE','true').lower()=='true'
BAR=os.getenv('BAR','5m'); TREND_BAR=os.getenv('TREND_BAR','15m')
MARGIN_USDT=Decimal(os.getenv('MARGIN_USDT','13')); LEVERAGE=Decimal(os.getenv('LEVERAGE','5'))
SL_PERCENT=Decimal(os.getenv('SL_PERCENT','0.4')); TP_PERCENT=Decimal(os.getenv('TP_PERCENT','0.8'))
FEE_SLIPPAGE_PCT=Decimal(os.getenv('FEE_SLIPPAGE_PCT','0.23'))
POLL_SECONDS=int(os.getenv('POLL_SECONDS','20')); MIN_SCORE=int(os.getenv('MIN_SCORE','7'))
ADX_MIN=Decimal(os.getenv('ADX_MIN','18')); VOLUME_MULT=Decimal(os.getenv('VOLUME_MULT','0.8')); ATR_MIN_PCT=Decimal(os.getenv('ATR_MIN_PCT','0.05'))
TD_MODE=os.getenv('TD_MODE','cross')
BREAK_EVEN_TRIGGER_PCT=Decimal(os.getenv('BREAK_EVEN_TRIGGER_PCT','0.30'))
BREAK_EVEN_OFFSET_PCT=Decimal(os.getenv('BREAK_EVEN_OFFSET_PCT','0.05'))
TRAIL_START_PCT=Decimal(os.getenv('TRAIL_START_PCT','0.50')); TRAIL_DISTANCE_PCT=Decimal(os.getenv('TRAIL_DISTANCE_PCT','0.30'))
PROTECTION_RETRY_SECONDS=int(os.getenv('PROTECTION_RETRY_SECONDS','5'))
SYMBOLS=[x.strip() for x in os.getenv('SYMBOLS','BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP,SOL-USDT-SWAP,SHIB-USDT-SWAP,XAU-USDT-SWAP').split(',') if x.strip()]
app=Flask(__name__); session=requests.Session(); state={}; lock=threading.Lock(); order_lock=threading.Lock(); server_offset_ms=0

def log(m): print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {m}',flush=True)
def dec(v): return Decimal(str(v))
def floor_step(v,s): return v if s<=0 else (v/s).to_integral_value(rounding=ROUND_DOWN)*s
def ceil_step(v,s): return v if s<=0 else (v/s).to_integral_value(rounding=ROUND_UP)*s

def public_get(path,params=None):
    r=session.get(BASE_URL+path,params=params,timeout=15); r.raise_for_status(); d=r.json()
    if d.get('code')!='0': raise RuntimeError(f'OKX PUBLIC {d.get("code")}: {d.get("msg")}')
    return d

def sync_time():
    global server_offset_ms
    a=int(time.time()*1000); d=public_get('/api/v5/public/time'); b=int(time.time()*1000); server=int(d['data'][0]['ts']); server_offset_ms=server-((a+b)//2)

def timestamp():
    return datetime.fromtimestamp((int(time.time()*1000)+server_offset_ms)/1000,tz=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

def sign(ts,method,path,body=''):
    return base64.b64encode(hmac.new(SECRET_KEY.encode(),(ts+method.upper()+path+body).encode(),hashlib.sha256).digest()).decode()

def private(method,path,payload=None,params=None):
    if not API_KEY or not SECRET_KEY or not PASSPHRASE: raise RuntimeError('OKX API credentials missing')
    method=method.upper(); reqpath=path; body=''
    if params: reqpath+='?'+urlencode([(str(k),str(v)) for k,v in params.items()])
    if payload is not None: body=json.dumps(payload,separators=(',',':'),ensure_ascii=False)
    ts=timestamp(); headers={'Content-Type':'application/json','OK-ACCESS-KEY':API_KEY,'OK-ACCESS-SIGN':sign(ts,method,reqpath,body),'OK-ACCESS-PASSPHRASE':PASSPHRASE,'OK-ACCESS-TIMESTAMP':ts}
    if DEMO: headers['x-simulated-trading']='1'
    r=session.request(method,BASE_URL+path,headers=headers,data=body or None,params=params,timeout=15)
    try:d=r.json()
    except Exception:d={'raw':r.text}
    if r.status_code>=400 or d.get('code')!='0': raise RuntimeError(f'OKX PRIVATE {r.status_code}: {d}')
    return d

def candles(symbol,bar='5m',limit=160):
    d=public_get('/api/v5/market/candles',{'instId':symbol,'bar':bar,'limit':str(limit)})
    return [{'ts':int(x[0]),'open':dec(x[1]),'high':dec(x[2]),'low':dec(x[3]),'close':dec(x[4]),'volume':dec(x[5]),'confirm':x[8] if len(x)>8 else '1'} for x in reversed(d.get('data',[]))]

def ticker(symbol):
    d=public_get('/api/v5/market/ticker',{'instId':symbol}); return dec(d['data'][0]['last'])
def ema(v,p):
    out=[None]*len(v)
    if len(v)<p:return out
    x=sum(v[:p],Decimal(0))/p; out[p-1]=x; k=Decimal(2)/(p+1)
    for i in range(p,len(v)): x=v[i]*k+x*(1-k); out[i]=x
    return out

def rsi(v,p):
    out=[None]*len(v)
    if len(v)<=p:return out
    gains=[max(v[i]-v[i-1],Decimal(0)) for i in range(1,len(v))]; losses=[max(v[i-1]-v[i],Decimal(0)) for i in range(1,len(v))]
    ag=sum(gains[:p],Decimal(0))/p; al=sum(losses[:p],Decimal(0))/p
    def rv(g,l): return Decimal(100) if l==0 else Decimal(100)-Decimal(100)/(1+g/l)
    out[p]=rv(ag,al)
    for j in range(p,len(gains)):
        ag=(ag*(p-1)+gains[j])/p; al=(al*(p-1)+losses[j])/p; out[j+1]=rv(ag,al)
    return out

def atr(cs,p=14):
    if len(cs)<=p:return None
    tr=[]
    for i in range(1,len(cs)): tr.append(max(cs[i]['high']-cs[i]['low'],abs(cs[i]['high']-cs[i-1]['close']),abs(cs[i]['low']-cs[i-1]['close'])))
    return sum(tr[-p:],Decimal(0))/p

def adx(cs,p=14):
    if len(cs)<p+2:return Decimal(0)
    plus=minus=tr=Decimal(0)
    for i in range(max(1,len(cs)-p),len(cs)):
        up=cs[i]['high']-cs[i-1]['high']; dn=cs[i-1]['low']-cs[i]['low']; plus+=up if up>dn and up>0 else 0; minus+=dn if dn>up and dn>0 else 0; tr+=cs[i]['high']-cs[i]['low']
    if tr==0:return Decimal(0)
    pi=plus/tr*100; mi=minus/tr*100; return abs(pi-mi)/(pi+mi)*100 if pi+mi else Decimal(0)

def macd(v):
    e12=ema(v,12); e26=ema(v,26); m=[(e12[i]-e26[i]) if e12[i] is not None and e26[i] is not None else None for i in range(len(v))]; valid=[x for x in m if x is not None]; sigvalid=ema(valid,9); sig=[None]*(len(v)-len(sigvalid))+sigvalid; return m,sig

def vwap(cs,p=20):
    q=cs[-p:]; vol=sum(x['volume'] for x in q); return sum(((x['high']+x['low']+x['close'])/3)*x['volume'] for x in q)/vol if vol else q[-1]['close']

def trend(symbol):
    cs=[x for x in candles(symbol,TREND_BAR,80) if x['confirm']=='1']; v=[x['close'] for x in cs]; e=ema(v,20); i=len(v)-1
    if len(v)<22 or e[i] is None:return 'flat'
    return 'bull' if v[i]>e[i] and e[i]>e[i-1] else 'bear' if v[i]<e[i] and e[i]<e[i-1] else 'flat'

def analyze(symbol):
    cs=[x for x in candles(symbol,BAR,160) if x['confirm']=='1'];
    if len(cs)<105:return {'signal':'NONE','score':0,'reason':'Not enough candles'}
    v=[x['close'] for x in cs]; i=len(cs)-1; e20=ema(v,20); r14=rsi(v,14); r100=rsi(v,100); a=atr(cs); ax=adx(cs); m,ms=macd(v); vw=vwap(cs); tr=trend(symbol)
    avg=sum((x['volume'] for x in cs[-21:-1]),Decimal(0))/20; vr=cs[i]['volume']/avg if avg else Decimal(0); ap=a/v[i]*100 if a else Decimal(0)
    buy=sell=0; reasons=[]
    def add(side,n,text):
        nonlocal buy,sell
        if side=='buy':buy+=n
        elif side=='sell':sell+=n
        reasons.append(text)
    if r14[i]>r100[i]:add('buy',2,'RSI bullish')
    elif r14[i]<r100[i]:add('sell',2,'RSI bearish')
    if r14[i-1]<=r100[i-1]<r14[i]:add('buy',1,'RSI crossover')
    elif r14[i-1]>=r100[i-1]>r14[i]:add('sell',1,'RSI crossunder')
    if v[i]>e20[i]:add('buy',1,'Above EMA20')
    else:add('sell',1,'Below EMA20')
    if e20[i]>e20[i-1]:add('buy',1,'EMA slope up')
    elif e20[i]<e20[i-1]:add('sell',1,'EMA slope down')
    if m[i] is not None and ms[i] is not None:
        if m[i]>ms[i]:add('buy',1,'MACD bullish')
        elif m[i]<ms[i]:add('sell',1,'MACD bearish')
    if v[i]>vw:add('buy',1,'Above VWAP')
    elif v[i]<vw:add('sell',1,'Below VWAP')
    if ax>=ADX_MIN:
        if buy>sell:add('buy',1,'ADX confirmed')
        elif sell>buy:add('sell',1,'ADX confirmed')
    if vr>=VOLUME_MULT:
        if buy>sell:add('buy',1,'Volume confirmed')
        elif sell>buy:add('sell',1,'Volume confirmed')
    if ap>=ATR_MIN_PCT:
        if buy>sell:add('buy',1,'ATR OK')
        elif sell>buy:add('sell',1,'ATR OK')
    if tr=='bull':add('buy',1,'15m bull')
    elif tr=='bear':add('sell',1,'15m bear')
    # simple structure / fake-break rejection
    prev=cs[i-1]; recent_high=max(x['high'] for x in cs[-21:-1]); recent_low=min(x['low'] for x in cs[-21:-1])
    if prev['high']>recent_high and prev['close']<recent_high:add('sell',1,'Fake breakout rejection')
    if prev['low']<recent_low and prev['close']>recent_low:add('buy',1,'Fake breakdown rejection')
    score=max(buy,sell); sig='NONE'
    if buy>sell and buy>=MIN_SCORE:sig='BUY'
    elif sell>buy and sell>=MIN_SCORE:sig='SELL'
    if (tr=='bull' and sig=='SELL') or (tr=='bear' and sig=='BUY'):sig='NONE'; reasons.append('Blocked by 15m trend')
    return {'signal':sig,'score':score,'buy':buy,'sell':sell,'entry':v[i],'rsi14':r14[i],'rsi100':r100[i],'ema20':e20[i],'macd':m[i],'macd_signal':ms[i],'vwap':vw,'adx':ax,'atr_pct':ap,'volume_ratio':vr,'trend15':tr,'reason':' | '.join(reasons)}

def instrument(symbol):
    d=public_get('/api/v5/public/instruments',{'instType':'SWAP','instId':symbol}); x=d['data'][0]
    return {'ctVal':dec(x['ctVal']),'lotSz':dec(x['lotSz']),'minSz':dec(x['minSz']),'tickSz':dec(x['tickSz'])}

def position(symbol):
    d=private('GET','/api/v5/account/positions',params={'instId':symbol}); rows=d.get('data',[]); return next((x for x in rows if dec(x.get('pos','0'))!=0),None)

def place_market(symbol,side):
    with order_lock:
        inst=instrument(symbol); px=ticker(symbol); notional=MARGIN_USDT*LEVERAGE; qty=floor_step(notional/(px*inst['ctVal']),inst['lotSz'])
        if qty<inst['minSz']:raise RuntimeError(f'{symbol}: calculated size below minimum')
        pos_side='long' if side=='buy' else 'short'; payload={'instId':symbol,'tdMode':TD_MODE,'side':side,'ordType':'market','sz':str(qty),'posSide':pos_side,'clOrdId':'v7_'+uuid.uuid4().hex[:20]}
        d=private('POST','/api/v5/trade/order',payload); log(f'{symbol} {side.upper()} opened | margin={MARGIN_USDT} | leverage={LEVERAGE}x | sz={qty} | order={d["data"][0].get("ordId")}'); return d

def protection(symbol,p):
    if not p:return
    # Client-side state for trailing/breakeven; actual position closing is handled by monitor.
    side='long' if p.get('posSide','')=='long' or p.get('side','')=='long' else 'short'; entry=dec(p.get('avgPx','0'))
    if entry<=0:return
    px=ticker(symbol); move=(px-entry)/entry*100 if side=='long' else (entry-px)/entry*100
    s=state.setdefault(symbol,{}); s['peak']=max(dec(s.get('peak',px)),px) if side=='long' else min(dec(s.get('peak',px)),px)
    if move>=BREAK_EVEN_TRIGGER_PCT:
        s['be']=entry*(1+FEE_SLIPPAGE_PCT/100+BREAK_EVEN_OFFSET_PCT/100) if side=='long' else entry*(1-FEE_SLIPPAGE_PCT/100-BREAK_EVEN_OFFSET_PCT/100)
    if move>=TRAIL_START_PCT:
        s['trail']=s['peak']*(1-TRAIL_DISTANCE_PCT/100) if side=='long' else s['peak']*(1+TRAIL_DISTANCE_PCT/100)
    stop=s.get('trail',s.get('be'))
    if stop:
        hit=px<=stop if side=='long' else px>=stop
        if hit:
            close_side='sell' if side=='long' else 'buy'; inst=instrument(symbol); qty=floor_step(abs(dec(p.get('pos','0'))),inst['lotSz'])
            if qty>0:
                private('POST','/api/v5/trade/order',{'instId':symbol,'tdMode':TD_MODE,'side':close_side,'ordType':'market','sz':str(qty),'posSide':side,'reduceOnly':'true','clOrdId':'v7_close_'+uuid.uuid4().hex[:16]}); log(f'{symbol} {side.upper()} protection close | price={px} | stop={stop}'); state.pop(symbol,None)

def worker():
    sync_time()
    log(f'V7 started | demo={DEMO} auto_trade={AUTO_TRADE} | margin={MARGIN_USDT} | leverage={LEVERAGE}x | fee/slippage={FEE_SLIPPAGE_PCT}%')
    while True:
        for symbol in SYMBOLS:
            try:
                p=position(symbol)
                if p: protection(symbol,p); continue
                a=analyze(symbol); log(f'{symbol}: {a["signal"]} {a["score"]}/10-ish | {a["reason"]}')
                if AUTO_TRADE and a['signal'] in ('BUY','SELL') and a['score']>=MIN_SCORE: place_market(symbol,'buy' if a['signal']=='BUY' else 'sell')
            except Exception as e: log(f'{symbol} ERROR: {e}')
        time.sleep(POLL_SECONDS)

@app.get('/')
def home():return jsonify({'bot':'V7','status':'running','demo':DEMO,'auto_trade':AUTO_TRADE,'margin_usdt':str(MARGIN_USDT),'leverage':str(LEVERAGE),'fee_slippage_pct':str(FEE_SLIPPAGE_PCT),'symbols':SYMBOLS})
@app.get('/health')
def health():return jsonify({'status':'ok','version':'V7'})

def start():
    threading.Thread(target=worker,daemon=True).start(); app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
if __name__=='__main__':start()
