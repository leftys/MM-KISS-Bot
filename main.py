from typing import Any, Tuple
import argparse
import asyncio
import logging
import requests

from starknet_py.net.full_node_client import FullNodeClient
from starknet_py.contract import Contract
from starknet_py.net.account.account import Account
from starknet_py.net.signer.stark_curve_signer import KeyPair
from starknet_py.net.models.chains import StarknetChainId

from cfg import REMUS_ADDRESS, STARKNET_RPC, WALLET_ADDRESS, SOURCE_DATA, NETWORK, MARKET_MAKER_CFG, MAX_FEE, DECIMALS

PATH_TO_KEYSTORE = "keystore.json"


def setup_logging(log_level: str):
    """Configures logging for the application."""
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=getattr(logging, log_level.upper(), "INFO"), format=log_format)

    
def parse_arguments():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="Main async script for the application.")
    parser.add_argument(
        "--log-level",
        type = str,
        default = "INFO",
        choices = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help = "Set the logging level"
    )
    parser.add_argument(
        "--account-password",
        type = str, 
        default = "",
        help = "Set the account password"
    )
    return parser.parse_args()


def get_account(account_password: str) -> Account:
    """Get a market makers account."""
    client = FullNodeClient(node_url = STARKNET_RPC)
    account = Account(
        client = client,
        address = WALLET_ADDRESS,
        key_pair = KeyPair.from_keystore(PATH_TO_KEYSTORE, account_password.encode('utf-8')),
        chain = StarknetChainId[NETWORK]
    )
    logging.info("Succesfully loaded account.")
    return account


def get_market_cfg(all_remus_cfgs: Any, market_id: int) -> Tuple[Any, Any]:
    market_id = 1
    market_cfg = all_remus_cfgs[0][0]
    market_maker_cfg = MARKET_MAKER_CFG[market_id]
    assert market_maker_cfg
    market_cfg = [x for x in all_remus_cfgs[0] if x[0] == market_id][0]
    assert market_cfg
    logging.info(f"Succesfully loaded market configs for market_id={market_id}.")
    return market_cfg, market_maker_cfg


async def claim_tokens(market_cfg, remus_contract) -> None:
    """
    Claims the unclaims tokens.

    TODO: This implementation is quite inefficient since the claim of USDC happens too often across many markets.
    (within the overall picture of the bot being run across markets).
    """
    for token_address in [market_cfg[1]['base_token'], market_cfg[1]['quote_token']]:
        claimable = await remus_contract.functions['get_claimable'].call(
            token_address = token_address,
            user_address = WALLET_ADDRESS
        )
        # logging.info(f'Claimable amount is {claimable} for token {token_address}.')
        if claimable[0]:
            logging.info(f'Claiming {claimable} for token {token_address}.')
            claim = await remus_contract.functions['claim'].invoke_v1(
                token_address = token_address,
                amount = claimable[0],
                max_fee = MAX_FEE
            )
            # TODO should we wait for acceptance?
            # await claim.wait_for_acceptance()
        logging.info(f'Claim done.')


async def get_position(market_cfg, account, asks, bids, base_token_contract, quote_token_contract):
    """
    MM bot position is its balance plus the remaining amount on present orders.
    # base_token_contract - mETH
    # quote_token_contract - mUSDC
    """
    balance_base = await base_token_contract.functions['balance_of'].call(account=WALLET_ADDRESS)
    amount_remaining_base = sum(x['amount_remaining'] for x in asks)
    total_possible_position_base = balance_base[0] + amount_remaining_base
    #
    balance_quote = await quote_token_contract.functions['balance_of'].call(account=WALLET_ADDRESS)
    amount_remaining_quote = sum(x['amount_remaining'] for x in bids)
    total_possible_position_quote = balance_quote[0] + amount_remaining_quote

    logging.debug(f"Queried user balance for market_id: {market_cfg[0]} as ({total_possible_position_base}, {total_possible_position_quote})")
    logging.debug(f"Queried user balance for market_id: {market_cfg[0]}")

    return total_possible_position_base, total_possible_position_quote


async def get_optimal_quotes(asks, bids, market_maker_cfg, market_cfg, fair_price, total_possible_position_base, total_possible_position_quote, spread):
    """
    If an existing quote has lower than market_maker_cfg['minimal_remaining_quote_size'] quantity, it is requoted.
    
    Order sizes are adjusted based on position imbalances:
    - If we have surplus of base tokens, increase bid sizes to reduce base token holdings
    - If we have surplus of quote tokens, increase ask sizes to reduce quote token holdings
    """
    to_be_canceled = []
    to_be_created = []

    try:
        base_decimals = DECIMALS[market_cfg[1]['base_token', 18]]  # for example ETH
        quote_decimals = DECIMALS[market_cfg[1]['quote_token', 18]]  # for example USDC
    except KeyError:
        # testnet
        base_decimals = 18
        quote_decimals = 18
    
    max_error = market_maker_cfg['max_error_to_spread'] * spread

    # Calculate position imbalances in quote currency
    base_value = total_possible_position_base * fair_price
    quote_value = total_possible_position_quote
    total_value = base_value + quote_value
    
    # Calculate size multipliers based on imbalances
    # If perfectly balanced, both multipliers will be 1.0
    # Maximum skew is 50% (multiplier range: 0.5 to 1.5)
    base_ratio = base_value / total_value if total_value > 0 else 0.5
    quote_ratio = quote_value / total_value if total_value > 0 else 0.5
    
    bid_size_multiplier = 1.5 - base_ratio  # More bids when base_ratio is low
    ask_size_multiplier = 1.5 - quote_ratio  # More asks when quote_ratio is low
    
    logging.info(f"Position ratios - Base: {base_ratio:.2f}, Quote: {quote_ratio:.2f}")
    logging.info(f"Size multipliers - Bids: {bid_size_multiplier:.2f}, Asks: {ask_size_multiplier:.2f}")
    
    for side, side_name in [(asks, 'ask'), (bids, 'bid')]:
        to_be_canceled_side = []
        to_be_created_side = []
    
        for order in side:
            if order['amount_remaining'] / 10**base_decimals * order['price'] / 10**base_decimals < market_maker_cfg['minimal_remaining_quote_size']:
                logging.info(f"Canceling order because of insufficient amount. amount: {order['amount_remaining']}")
                logging.debug(f"Canceling order because of insufficient amount. order: {order}")
                to_be_canceled_side.append(order)
                continue
            if (
                (1 - max_error > order['price'] / 10**base_decimals / fair_price)
                or
                (order['price'] / 10**base_decimals / fair_price > 1 + max_error)
            ):
                logging.info(f"Canceling order because of incorrect price. fair_price: {fair_price}, order price: {order['price'] / 10**base_decimals}")
                logging.debug(f"Canceling order because of incorrect price. order: {order}")
                to_be_canceled_side.append(order)
        # If there is too many orders in the market that are not being canceled, cancel them at random. This can happen due to transactions failures.
        if len(side) - len(to_be_canceled_side) > 1:
            remainers = []
            for order in side:
                if order not in to_be_canceled_side:
                    remainers.append(order)
            to_be_canceled_side.extend(remainers[1:])
        to_be_canceled.extend(to_be_canceled_side)
    
        # Create order if there is no order
        if len(to_be_canceled_side) == len(side):
            if side_name == 'ask':
                optimal_price = int(fair_price * (1 + spread) * 10**base_decimals)
                optimal_price = optimal_price // market_cfg[1]['tick_size']
                optimal_price = optimal_price * market_cfg[1]['tick_size'] + market_cfg[1]['tick_size']
                size_multiplier = ask_size_multiplier
            else:
                optimal_price = int(fair_price * (1 - spread) * 10**base_decimals)
                optimal_price = optimal_price // market_cfg[1]['tick_size']
                optimal_price = optimal_price * market_cfg[1]['tick_size']
                size_multiplier = bid_size_multiplier
    
            # Apply size multiplier to adjust for imbalances
            optimal_amount = market_maker_cfg['order_dollar_size'] * size_multiplier / (optimal_price / 10**base_decimals)
            optimal_amount = optimal_amount // market_cfg[1]['lot_size']
            optimal_amount = optimal_amount * market_cfg[1]['lot_size']
    
            order = {
                'order_side': side_name,
                'amount': int(optimal_amount),
                'price': optimal_price
            }
            to_be_created.append(order)
    logging.info(f"Optimal quotes calculated: to_be_canceled: {len(to_be_canceled)}, to_be_created: {len(to_be_created)}")
    logging.debug(f"Optimal quotes calculated: to_be_canceled: {to_be_canceled}, to_be_created: {to_be_created}")
    return to_be_canceled, to_be_created


async def setup_unlimited_approvals(account: Account, remus_contract, market_cfg, base_token_contract, quote_token_contract):
    """Set up unlimited approvals for base and quote tokens."""
    logging.info("Setting up unlimited approvals for tokens...")
    max_uint = 2**256 - 1
    nonce = await account.get_nonce()
    
    # Approve base token
    await (await base_token_contract.functions['approve'].invoke_v1(
        spender=int(REMUS_ADDRESS, 16),
        amount=max_uint,
        max_fee=MAX_FEE,
        nonce=nonce
    )).wait_for_acceptance()
    logging.info(f"Set unlimited approval for base token: {market_cfg[1]['base_token']}")
    
    # Approve quote token
    await (await quote_token_contract.functions['approve'].invoke_v1(
        spender=int(REMUS_ADDRESS, 16),
        amount=max_uint,
        max_fee=MAX_FEE,
        nonce=nonce + 1
    )).wait_for_acceptance()
    logging.info(f"Set unlimited approval for quote token: {market_cfg[1]['quote_token']}")


async def update_quotes(account: Account, market_cfg, remus_contract, to_be_canceled, to_be_created, base_token_contract, quote_token_contract):
    nonce = await account.get_nonce()
    for i, order in enumerate(to_be_canceled):
        await (await remus_contract.functions['delete_maker_order'].invoke_v1(
            maker_order_id=order['maker_order_id'],
            max_fee=MAX_FEE,
            nonce = nonce + i
        )).wait_for_acceptance()
        logging.info(f"Canceling: {order['maker_order_id']}")
    
    for i, order in enumerate(to_be_created):
        if order['order_side'] == 'ask':
            target_token_address = market_cfg[1]['base_token']
            order_side = 'Ask'
        else:
            target_token_address = market_cfg[1]['quote_token']
            order_side = 'Bid'

        logging.info(f"Soon to sumbit order: q: {order['amount']}, p: {order['price']}, s: {order_side}")
        await (await remus_contract.functions['submit_maker_order'].invoke_v1(
            market_id=1,
            target_token_address=target_token_address,
            order_price = order['price'],
            order_size = order['amount'],
            order_side = (order_side, None),
            order_type = ('Basic', None),
            time_limit = ('GTC', None),
            max_fee=MAX_FEE,
            nonce = nonce + len(to_be_canceled) + i
        )).wait_for_acceptance()
        logging.info(f"Submitting order: q: {order['amount']}, p: {order['price']}, s: {order_side}")
    logging.info('Done with order changes')


async def cancel_all_orders(account: Account, remus_contract):
    """Cancel all open orders for the account."""
    logging.info("Canceling all open orders...")
    try:
        my_orders = await remus_contract.functions['get_all_user_orders'].call(user=WALLET_ADDRESS)
        if not my_orders[0]:
            logging.info("No open orders to cancel")
            return

        nonce = await account.get_nonce()
        for i, order in enumerate(my_orders[0]):
            await (await remus_contract.functions['delete_maker_order'].invoke_v1(
                maker_order_id=order['maker_order_id'],
                max_fee=MAX_FEE,
                nonce=nonce + i
            )).wait_for_acceptance()
            logging.info(f"Canceled order: {order['maker_order_id']}")
        
        logging.info("Successfully canceled all orders")
    except Exception as e:
        logging.error(f"Error while canceling orders: {e}")
        raise


async def async_main():
    """Main async execution function."""
    args = parse_arguments()
    setup_logging(args.log_level)

    logging.info("Starting Simple Stupid Market Maker")

    account = get_account(args.account_password)
    remus_contract = await Contract.from_address(address = REMUS_ADDRESS, provider = account)
    all_remus_cfgs = await remus_contract.functions['get_all_market_configs'].call()
    
    # Initialize contracts and set up approvals for the first market
    for market_id in [x[0] for x in all_remus_cfgs[0] if x[0] in MARKET_MAKER_CFG]:
        market_cfg, market_maker_cfg = get_market_cfg(all_remus_cfgs, market_id)
        base_token_contract = await Contract.from_address(address = market_cfg[1]['base_token'], provider = account)
        quote_token_contract = await Contract.from_address(address = market_cfg[1]['quote_token'], provider = account)
        await setup_unlimited_approvals(account, remus_contract, market_cfg, base_token_contract, quote_token_contract)
    
        while True:
            await asyncio.sleep(1)  # Example async operation
            for market_id in [x[0] for x in all_remus_cfgs[0] if x[0] in MARKET_MAKER_CFG]:
                try:
                    market_cfg, market_maker_cfg = get_market_cfg(all_remus_cfgs, market_id)

                    # 1) Claim tokens
                    # TODO ideally the claim would happen after the order deletion.
                    await claim_tokens(market_cfg, remus_contract)

                    # 2) Get prices
                    r = requests.get(SOURCE_DATA[market_id])
                    data = r.json()
                    trades = sorted(data, key = lambda x: x['T'])
                    fair_price = float(trades[-1]['p'])
                    abs_return = abs(float(trades[-1]['p']) / float(trades[0]['p']) - 1)
                    spread = max(market_maker_cfg['min_distance_from_FP'], abs_return)

                    logging.info(f'Fair price queried: {fair_price}.')

                    # 3) Get orders
                    my_orders = await remus_contract.functions['get_all_user_orders'].call(user=WALLET_ADDRESS)

                    bids = [x for x in my_orders[0] if x['market_id'] == market_id and x['order_side'].variant == 'Bid']
                    asks = [x for x in my_orders[0] if x['market_id'] == market_id and x['order_side'].variant == 'Ask']
                    logging.debug(f'My remaining orders queried: {bids}, {asks}.')

                    # 4) Get position (balance of + open orders)
                    base_token_contract = await Contract.from_address(address = market_cfg[1]['base_token'], provider = account)
                    quote_token_contract = await Contract.from_address(address = market_cfg[1]['quote_token'], provider = account)
                    total_possible_position_base, total_possible_position_quote = await get_position(
                        market_cfg, account, asks, bids, base_token_contract, quote_token_contract
                    )

                    # 5) Calculate optimal quotes
                    to_be_canceled, to_be_created = await get_optimal_quotes(asks, bids, market_maker_cfg, market_cfg, fair_price, total_possible_position_base, total_possible_position_quote, spread)

                    # 6) update quotes
                    await update_quotes(account, market_cfg, remus_contract, to_be_canceled, to_be_created, base_token_contract, quote_token_contract)
                except asyncio.CancelledError:
                    pass
                except KeyboardInterrupt:
                    logging.info("Application stopped by user")
                    return
                except Exception as e:
                    logging.error("An error occurred: %s", str(e), exc_info=True)
                    # sys.exit(1)
                finally:
                    await cancel_all_orders(account, remus_contract)

if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logging.info("Application stopped by user #2")
