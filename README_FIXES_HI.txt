Black Fire Store - KeyAuth + catalog fixes

Is build mein:
1. KeyAuth ke naye account ka username har order ke liye random 8-character letters+numbers hoga.
2. Password hamesha `1` rahega.
3. Agar generated username KeyAuth mein pehle se kisi doosre password/account ke saath hai, system us username ko reuse nahi karega; naya random username try karega.
4. Usi order ki delivery retry mein agar usi 8-character username par password `1` aur required subscription milti hai, account ko extend karke wahi credentials rakhega.
5. Telegram shop ke products Main Admin Panel ke shared product catalog se aate hain.
6. Discord shop mein sirf Discord Seller Admin Panel se publish kiye gaye products aayenge.
7. Discord Seller products aur Telegram Main Admin products dono ke KeyAuth credentials same format follow karenge: random 8-character username, password `1`.
8. Guild Glory/Main Admin products Discord shop mein nahi dikhaye jayenge.

Install:
- Bot ko CTRL+C se stop karein.
- Current folder aur database ka backup lein.
- Is ZIP ke source files ko project folder mein copy/replace karein.
- Apna `.env` aur existing database/orders data preserve karein; ZIP mein `.env` ya database nahi hai.
- Run:
  python -m py_compile *.py
  python main.py

Important:
- Python syntax compilation aur mocked username-generation test pass hue.
- Real KeyAuth, Telegram, Discord, Razorpay, aur FFGlory live transaction end-to-end test nahi hua.
- Aapke pasted logs mein Telegram bot token publicly expose hua hai. BotFather mein `/revoke` se token rotate karein aur naya token local `.env` mein set karein. Naya token chat mein share na karein.


LATEST PATCH (2026-10-25):
- Fixed missing Discord seller-product duration handler (_handle_keyauth_product_selected) which caused NameError on selection.
- Added Main Admin > My Keys: list Telegram/Main Admin KeyAuth accounts, buy date, expiry, username/password, Reset and permanent Delete.
- Reset deletes/recreates the saved KeyAuth user with the original purchased number of days; Delete removes the remote KeyAuth user and hides the account from active list.
- Main Admin manual KeyAuth generation now generates a unique random 8-character alphanumeric username, password 1, verifies account/subscription, and records it in My Keys.
- Uses KeyAuth Seller API endpoints documented at https://docs.keyauth.cc/sellerapi/users/create-new-user and /users/retrieve-user-data.
- Live KeyAuth API/payment testing requires the user's valid Seller API key and actual KeyAuth subscription/application.
