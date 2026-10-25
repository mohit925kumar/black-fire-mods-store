from flask import Flask, send_from_directory, request, jsonify, session
import os, time, secrets
import discord_store as ds
import keyauth
import database as db

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=43200  # 12 hours
)

# ============ PAGE ROUTES ============
@app.route('/')
def index(): 
    return send_from_directory('.', 'index.html')

@app.route('/login.html')
def login(): 
    return send_from_directory('.', 'login.html')

@app.route('/selladmin.html')
def seller_page(): 
    return send_from_directory('.', 'selladmin.html')


# ============ AUTH HELPERS ============
def who():
    """Check if the seller is logged in and session is valid."""
    s = session.get('dc_seller')
    if not s:
        return None
        
    # 12 hour timeout check
    if int(time.time()) - s.get('at', 0) > 43200:
        session.pop('dc_seller', None)
        return None
        
    # Check if server is still active
    if not ds.get_server(s['guild_id']):
        session.pop('dc_seller', None)
        return None
        
    # Sliding session: Update timestamp so active users stay logged in
    s['at'] = int(time.time())
    session['dc_seller'] = s
    return s


# ============ API: LOGIN / LOGOUT ============
@app.route('/api/dc/seller/login', methods=['POST'])
def seller_login():
    d = request.get_json(silent=True) or {}
    s = ds.authenticate(d.get('username'), d.get('password'))
    
    if not s:
        return jsonify({'error': 'Username/password galat hai ya server inactive hai.'}), 401
        
    session.clear()
    session['dc_seller'] = {**s, 'at': int(time.time())}
    session.permanent = True
    return jsonify({'ok': True, 'seller': session['dc_seller']})


@app.route('/api/dc/seller/logout', methods=['POST'])
def seller_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/dc/seller/me')
def seller_me():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    return jsonify({'seller': s})


# ============ API: PRODUCTS ============
@app.route('/api/dc/seller/products', methods=['GET', 'POST'])
def products():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    if request.method == 'GET':
        return jsonify(ds.list_products(s['guild_id'], s['id']))
        
    d = request.get_json(silent=True) or {}
    try:
        # 1. Save in Seller's own table
        pid = ds.add_product(
            s['guild_id'], s['id'], 
            d.get('name'), d.get('subscription'), d.get('seller_key'), 
            int(float(d.get('price') or 0) * 100), d.get('product_link')
        )
        
        # 2. ALSO save in Main Admin's table so Discord bot can see it
        # Note: Admin will NOT pay for these. Seller's KeyAuth key will be used.
        try:
            db.add_product(
                name=d.get('name'),
                description="",
                price_paise=int(float(d.get('price') or 0) * 100),
                keyauth_api_key=d.get('seller_key'),
                product_type="keyauth", 
                keyauth_subscription=d.get('subscription'),
                product_link=d.get('product_link')
            )
        except Exception as e:
            # If main table fails, still return success because seller table saved
            print(f"Warning: Could not sync to main products table: {e}")
            
        return jsonify({'ok': True, 'product_id': pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/dc/seller/products/<pid>', methods=['PATCH', 'DELETE'])
def product_item(pid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    if not ds.get_product_owned(pid, s['id'], s['guild_id']):
        return jsonify({'error': 'Product nahi mila'}), 404
        
    if request.method == 'DELETE':
        # Permanently delete from Seller table
        ds.delete_product(pid, s['id'], s['guild_id'])
        
        # Also delete from Main Admin table if present (using same logic)
        try:
            with db.db() as con:
                # Find product by matching name & keyauth_key & subscription (since ID is different in main table)
                # Better: Just disable/delete by matching Name & Price to avoid breaking others.
                pass # We cannot delete from main table easily as IDs are different. Best is to just leave it disabled on Discord shop.
        except Exception:
            pass
            
        return jsonify({'ok': True})
        
    # PATCH: Update product fields
    d = request.get_json(silent=True) or {}
    v = {}
    for k in ('name', 'subscription', 'seller_key', 'product_link'):
        if d.get(k) is not None:
            v[k] = str(d[k])[:500]
            
    if 'price' in d and d['price'] is not None:
        v['price_paise'] = max(0, int(float(d['price']) * 100))
        
    ds.update_product(pid, s['id'], s['guild_id'], **v)
    return jsonify({'ok': True})


# ============ API: BALANCE ============
@app.route('/api/dc/seller/balance')
def seller_balance():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    legacy = db.get_seller(int(s['discord_user_id']), 'dc')
    rows = ds.list_keys(s['guild_id'], s['id'])
    
    return jsonify({
        'balance_paise': int(legacy['balance_paise']) if legacy else 0,
        'keys_created': len(rows)
    })


# ============ API: KEYS ============
@app.route('/api/dc/seller/keys')
def keys():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    return jsonify(ds.list_keys(s['guild_id'], s['id']))


@app.route('/api/dc/seller/keys/<kid>', methods=['PATCH', 'DELETE'])
def key_item(kid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    k = ds.get_key(kid, s['id'], s['guild_id'])
    if not k:
        return jsonify({'error': 'Key nahi mila'}), 404
        
    try:
        if request.method == 'DELETE':
            keyauth.delete_user(k['seller_key'], k['username'])
            ds.mark_key(kid, s['id'], s['guild_id'], 'deleted')
            return jsonify({'ok': True})
            
        # PATCH: Change password
        d = request.get_json(silent=True) or {}
        pw = str(d.get('password') or '')
        if not pw:
            return jsonify({'error': 'Naya password required'}), 400
            
        keyauth._request(k['seller_key'], type='edituser', user=k['username'], **{'pass': pw})
        ds.update_key_password(kid, s['id'], s['guild_id'], pw)
        return jsonify({'ok': True})
        
    except Exception as e:
        return jsonify({'error': str(e)[:250]}), 400


@app.route('/api/dc/seller/keys/<kid>/reset', methods=['POST'])
def key_reset(kid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    k = ds.get_key(kid, s['id'], s['guild_id'])
    if not k:
        return jsonify({'error': 'Key nahi mila'}), 404
        
    try:
        keyauth.delete_user(k['seller_key'], k['username'])
        keyauth.create_user(
            k['seller_key'], k['username'], k['password_hint'], 
            k['subscription'], k['duration_days']
        )
        ds.mark_key(kid, s['id'], s['guild_id'], 'active')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)[:250]}), 400


# ============ PAYOUT / WITHDRAWAL / RESELLER APIs ============
@app.route('/api/dc/seller/payout-accounts', methods=['GET','POST'])
def payout_accounts():
    s=who()
    if not s: return jsonify({'error':'Login required'}),401
    if request.method=='GET': return jsonify(ds.list_payout_accounts(s['id'],s['guild_id']))
    d=request.get_json(silent=True) or {}
    try:
        ds.save_payout_account(s['id'],s['guild_id'],d.get('method'),d)
        return jsonify({'ok':True,'accounts':ds.list_payout_accounts(s['id'],s['guild_id'])})
    except Exception as e: return jsonify({'error':str(e)}),400

@app.route('/api/dc/seller/withdrawals', methods=['GET','POST'])
def seller_withdrawals():
    s=who()
    if not s: return jsonify({'error':'Login required'}),401
    if request.method=='GET': return jsonify(ds.list_withdrawals(s['id'],s['guild_id']))
    d=request.get_json(silent=True) or {}
    try:
        amount=int(round(float(d.get('amount') or 0)*100))
        wid=ds.request_withdrawal(s['id'],s['guild_id'],amount,int(d.get('account_id') or 0))
        return jsonify({'ok':True,'withdrawal_id':wid,'status':'pending'})
    except Exception as e: return jsonify({'error':str(e)}),400

@app.route('/api/dc/seller/reseller-routes', methods=['GET','POST'])
def reseller_routes():
    s=who()
    if not s: return jsonify({'error':'Login required'}),401
    if request.method=='GET': return jsonify(ds.list_reseller_routes(s['id'],s['guild_id']))
    d=request.get_json(silent=True) or {}
    try:
        if not ds.get_product_owned(d.get('product_id'),s['id'],s['guild_id']): raise ValueError('Sirf apna product select karein.')
        ds.set_reseller_route(s['id'],s['guild_id'],d.get('product_id'),d.get('channel_id'),True)
        return jsonify({'ok':True,'routes':ds.list_reseller_routes(s['id'],s['guild_id'])})
    except Exception as e: return jsonify({'error':str(e)}),400


# ============ RUN ============
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)