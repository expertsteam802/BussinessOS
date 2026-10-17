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

## Important

- Back up `data/business_os.db` before replacing the app.
- The browser UI and backend are paired; do not copy only the JavaScript file over an older HTML version.
- Online sync and email resets do not activate until the relevant service settings are configured.
