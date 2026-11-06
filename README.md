# Business OS — Updated features

## Windows install / update

1. Back up your current project and the `data` folder first.
2. Replace `app.py`, `templates/index.html`, `static/js/app.js`, and `static/css/style.css` with the matching files in this package. **Replace all four together** so the page and JavaScript use the same form IDs; this prevents the `Cannot read properties of null (reading 'reset')` error caused by mixing versions.
3. Keep your existing `.env` file. Do not put your secret AI key into browser JavaScript or share it in chat.
4. In the project folder run `python -m pip install -r requirements.txt`.
5. Stop the old server with Ctrl+C and run `python app.py`. Open `http://127.0.0.1:5000` and reload the page.

## Features in this update

- Register items in a popup; edit item name, barcode/code, category, stock, cost, selling price and low-stock level.
- Delete an item from inventory. Existing sales history remains.
- Item IDs are generated automatically. If no barcode is supplied, an item code such as `ITEM-000001` is assigned.
- USB/Bluetooth barcode scanners that type into the focused field are supported. Scan while registering to fill the barcode, or scan during checkout to select the saved item. Scanning a new barcode does not automatically fetch product details from the internet; enter the item details and save it.
- Search sales by barcode, generated item code, item ID or name. Press Enter to choose a matching item.
- Press Enter in the AI message box to send; use Shift+Enter for a new line.
- Change business name, owner/contact name, business type and display currency in Settings. Currency selection changes display formatting only; it does not convert existing monetary amounts using exchange rates.
- Local records remain on the PC. Optional online backup/restore requires Supabase settings. Email password reset requires email settings.

## Supabase cloud sync

A dedicated Supabase project has been provisioned for Business OS. The app already contains a local-first snapshot sync adapter; the database table it uses is now created in that project.

1. Copy `.env.example` to `.env` in the project root.
2. Set `SUPABASE_URL` to the project URL shown in your Supabase dashboard.
3. Set `SUPABASE_SERVICE_ROLE_KEY` to the project's **secret/service-role key** from Supabase Dashboard → Project Settings → API Keys. Use the secret key only in this Python server-side `.env` file. Never put it in HTML, JavaScript, a desktop/web client bundle, GitHub, screenshots, or messages.
4. Keep `.env` private. It is ignored by Git. Use a unique `BUSINESS_OS_SECRET` value for the Flask session.
5. Install dependencies with `python -m pip install -r requirements.txt`, then restart with `python app.py`.
6. Log in and open Settings → backup/sync status. The app should report cloud sync as configured after a successful sync.

The local SQLite database remains the working database, so checkout can continue offline. When online, the app uploads a JSON snapshot of the account, products, sales, and expenses. Logging in on another PC can restore that snapshot to its local database. This is snapshot backup/restore, not live multi-device row-level synchronization; avoid editing the same account on two PCs at once.

The Supabase table has Row Level Security enabled and grants no access to the public `anon` or `authenticated` roles. The app's server-side secret key is required for sync. The secret key bypasses RLS, so keep it on the trusted Python backend only.

## Important

- Back up `data/business_os.db` before replacing the app.
- The browser UI and backend are paired; do not copy only the JavaScript file over an older HTML version.
- Online sync and email resets do not activate until the relevant service settings are configured.
- Do not commit `.env` or any service-role/secret key to GitHub.
