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
        # Default price ₹1 if not provided
        base_price = int(float(d.get('price') or 0) * 100)
        if base_price <= 0:
            base_price = 100

        pid = ds.add_product(
            s['guild_id'], s['id'], 
            d.get('name'), d.get('subscription'), d.get('seller_key'), 
            base_price, d.get('product_link')
        )
        
        # Auto-create default duration options
        ds.seed_default_options(pid)
            
        return jsonify({'ok': True, 'product_id': pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/dc/seller/products/<pid>', methods=['GET', 'PATCH', 'DELETE'])
def product_item(pid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    if not ds.get_product_owned(pid, s['id'], s['guild_id']):
        return jsonify({'error': 'Product nahi mila'}), 404
        
    if request.method == 'GET':
        # Include duration options in product details
        product = dict(ds.get_product_owned(pid, s['id'], s['guild_id']))
        product['duration_options'] = ds.get_duration_options(pid)
        return jsonify(product)
        
    if request.method == 'DELETE':
        ds.delete_product(pid, s['id'], s['guild_id'])
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

# Publish Product (Discord Visibility)
@app.route('/api/dc/seller/products/<pid>/publish', methods=['POST'])
def product_publish(pid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    product = ds.get_product_owned(pid, s['id'], s['guild_id'])
    if not product:
        return jsonify({'error': 'Product nahi mila'}), 404
        
    try:
        # Discord Seller catalog is kept separate from the Main Admin/Telegram catalog.
        # This avoids duplicate products and preserves each seller product's own duration prices.
        ds.publish_product(pid, s['id'], s['guild_id'])
        return jsonify({'ok': True, 'message': 'Product published to Discord!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# Unpublish Product
@app.route('/api/dc/seller/products/<pid>/unpublish', methods=['POST'])
def product_unpublish(pid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    try:
        ds.unpublish_product(pid, s['id'], s['guild_id'])
        return jsonify({'ok': True, 'message': 'Product unpublished from Discord!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ============ API: DURATION OPTIONS ============
@app.route('/api/dc/seller/products/<pid>/duration', methods=['GET', 'POST'])
def product_durations(pid):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    
    if not ds.get_product_owned(pid, s['id'], s['guild_id']):
        return jsonify({'error': 'Product nahi mila'}), 404
    
    if request.method == 'GET':
        return jsonify(ds.get_duration_options(pid))
    
    d = request.get_json(silent=True) or {}
    try:
        option_id = ds.add_duration_option(
            pid,
            ds.duration_label(int(d.get('duration_days'))),
            int(d.get('duration_days')),
            int(float(d.get('price')) * 100)
        )
        return jsonify({'ok': True, 'option_id': option_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/dc/seller/products/<pid>/duration/<option_id>', methods=['PATCH', 'DELETE'])
def duration_item(pid, option_id):
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    
    if not ds.get_product_owned(pid, s['id'], s['guild_id']):
        return jsonify({'error': 'Product nahi mila'}), 404
    
    if request.method == 'DELETE':
        ds.delete_duration_option(option_id)
        return jsonify({'ok': True})
    
    d = request.get_json(silent=True) or {}
    try:
        ds.update_duration_price(option_id, int(float(d.get('price')) * 100))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ============ API: BALANCE & SEARCH ============
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

# User Search
@app.route('/api/dc/seller/search-user')
def seller_search_user():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    uid = request.args.get('user_id')
    if not uid:
        return jsonify({'error': 'User ID required'}), 400
        
    user = db.get_user(int(uid), 'dc')
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    orders = db.list_user_orders(int(uid), 'dc', 100)
    
    return jsonify({
        'user': dict(user),
        'orders': [dict(o) for o in orders]
    })

# Add Balance (to a specific Discord User)
@app.route('/api/dc/seller/add-balance', methods=['POST'])
def seller_add_balance():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
        
    d = request.get_json(silent=True) or {}
    uid = d.get('user_id')
    amount = float(d.get('amount') or 0)
    
    if not uid or amount <= 0:
        return jsonify({'error': 'Valid User ID and Amount required'}), 400
        
    db.add_balance(int(uid), 'dc', int(amount * 100))
    return jsonify({'ok': True, 'message': 'Balance added successfully'})


# ============ API: KEYS ============
@app.route('/api/dc/seller/keys')
def keys():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    return jsonify(ds.list_keys(s['guild_id'], s['id']))


@app.route('/api/dc/seller/keys', methods=['POST'])
def generate_seller_key():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    d = request.get_json(silent=True) or {}
    pid = str(d.get('product_id') or '').strip()
    username = str(d.get('username') or '').strip()
    password = str(d.get('password') or '')
    try:
        days = int(d.get('days') or 0)
        product = ds.get_product_owned(pid, s['id'], s['guild_id'])
        if not product:
            raise ValueError('Apna project/product select karein.')
        if not username or len(username) > 64 or not password:
            raise ValueError('Username aur password dono required hain.')
        if days < 1 or days > 36500:
            raise ValueError('Days 1 se 36500 ke beech hona chahiye.')
        keyauth.create_user(product['seller_key'], username, password, product['subscription'], days)
        label = ds.duration_label(days)
        kid = ds.add_key(s['guild_id'], s['id'], pid, username, password, label, days)
        return jsonify({'ok': True, 'key_id': kid, 'message': 'Key/project account successfully generate ho gaya.'})
    except Exception as e:
        return jsonify({'error': str(e)[:250]}), 400


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


# ============ API: RULES ============
@app.route('/api/dc/seller/rules', methods=['GET'])
def get_rules():
    s = who()
    if not s:
        return jsonify({'error': 'Login required'}), 401
    
    rules = db.list_rules()
    return jsonify([dict(r) for r in rules])


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