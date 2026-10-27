# Patched for fennel-invest-api==1.2.0 (email + OTP login instead of PAT tokens)
import asyncio
import os
import traceback
from asyncio import AbstractEventLoop
from typing import TYPE_CHECKING, cast

from dotenv import load_dotenv
from fennel_invest_api import Fennel

from src.helper_api import Brokerage, StockOrder, get_otp_from_discord, print_all_holdings, print_and_discord

if TYPE_CHECKING:
    from discord.ext.commands import Bot


def _get_approved_accounts(fb: Fennel) -> list[dict]:
    """Query Fennel accounts using 1.2.0's union-aware GraphQL query."""
    headers = fb.endpoints.build_headers(fb.Bearer)
    resp = fb.session.post(
        fb.endpoints.graphql,
        headers=headers,
        data=fb.endpoints.account_ids_query(),
    ).json()
    if not resp.get("data") or not resp["data"].get("user"):
        msg = f"Fennel account query failed: {resp.get('errors', resp)}"
        raise Exception(msg)
    return sorted(
        (a for a in resp["data"]["user"]["accounts"] if a.get("status") == "APPROVED"),
        key=lambda a: a["created"],
    )


def fennel_init(loop: AbstractEventLoop | None = None, bot_obj: "Bot | None" = None) -> Brokerage | None:
    """Initialize Fennel API (email/OTP login)."""
    load_dotenv()
    fennel_obj = Brokerage("Fennel")
    if not os.getenv("FENNEL"):
        print("Fennel not found, skipping...")
        return None
    big_fennel = os.environ["FENNEL"].strip().split(",")
    print("Logging in to Fennel...")
    for index, account in enumerate(big_fennel):
        name = f"Fennel {index + 1}"
        account = account.strip()
        try:
            fb = Fennel(filename=f"fennel{index + 1}.pkl", path="./creds/")
            if bot_obj is None or loop is None:
                # CLI: library prompts for the emailed code via input()
                fb.login(email=account, wait_for_code=True)
            else:
                try:
                    fb.login(email=account, wait_for_code=False)
                except Exception as e:
                    if "2FA" not in str(e):
                        raise
                    otp_code = asyncio.run_coroutine_threadsafe(
                        get_otp_from_discord(bot_obj, name, timeout=300, loop=loop),
                        loop,
                    ).result()
                    if otp_code is None:
                        msg = "No 2FA code found"
                        raise Exception(msg) from e
                    fb.login(email=account, wait_for_code=False, code=otp_code)
            accounts = _get_approved_accounts(fb)
            fennel_obj.set_logged_in_object(name, fb, "fb")
            for a in accounts:
                b = fb.get_portfolio_summary(a["id"])
                fennel_obj.set_account_number(name, a["name"])
                fennel_obj.set_account_totals(name, a["name"], b["cash"]["balance"]["canTrade"])
                fennel_obj.set_logged_in_object(name, a["id"], a["name"])
                print(f"Found {a['name']}")
            print(f"{name}: Logged in")
        except Exception as e:
            print_and_discord(f"Error logging into {name}: {e}", loop)
            print(traceback.format_exc())
            continue
    if not fennel_obj.get_account_numbers():
        print_and_discord("Failed to log into any Fennel account", loop)
        return None
    print("Logged into Fennel!")
    return fennel_obj


def fennel_holdings(fbo: Brokerage, loop: AbstractEventLoop | None = None) -> None:
    """Retrieve and display all Fennel account holdings."""
    for key in fbo.get_account_numbers():
        obj = cast("Fennel", fbo.get_logged_in_objects(key, "fb"))
        for account in fbo.get_account_numbers(key):
            account_id = fbo.get_logged_in_objects(key, account)
            try:
                for holding in obj.get_stock_holdings(account_id) or []:
                    qty = float(holding["investment"]["ownedShares"])
                    if qty == 0:
                        continue
                    sym = holding["security"]["ticker"]
                    price = holding["security"]["currentStockPrice"]
                    if price is None:
                        price = "N/A"
                    fbo.set_holdings(key, account, sym, qty, price)
            except Exception as e:
                print_and_discord(f"Error getting Fennel holdings: {e}", loop)
                print(traceback.format_exc())
                continue
    print_all_holdings(fbo, loop, mask_account_number=False)


def fennel_transaction(fbo: Brokerage, order_obj: StockOrder, loop: AbstractEventLoop | None = None) -> None:
    """Handle Fennel API transactions."""
    print()
    print("==============================")
    print("Fennel")
    print("==============================")
    print()
    for s in order_obj.get_stocks():
        for key in fbo.get_account_numbers():
            print_and_discord(
                f"{key}: {order_obj.get_action()}ing {order_obj.get_amount()} of {s}",
                loop,
            )
            for account in fbo.get_account_numbers(key):
                obj = cast("Fennel", fbo.get_logged_in_objects(key, "fb"))
                account_id = fbo.get_logged_in_objects(key, account)
                try:
                    order = obj.place_order(
                        account_id=account_id,
                        ticker=s,
                        quantity=order_obj.get_amount(),
                        side=order_obj.get_action(),
                        dry_run=order_obj.get_dry(),
                    )
                    if order_obj.get_dry():
                        message = "Dry Run Success" if order.get("dry_run_success", False) else "Dry Run Failed"
                    else:
                        message = "Success"
                        if order.get("data", {}).get("createOrder") != "pending":
                            message = order.get("data", {}).get("createOrder", order)
                    print_and_discord(
                        f"{key}: {order_obj.get_action()} {order_obj.get_amount()} of {s} in {account}: {message}",
                        loop,
                    )
                except Exception as e:
                    print_and_discord(f"{key} {account}: Error placing order: {e}", loop)
                    print(traceback.format_exc())
                    continue
