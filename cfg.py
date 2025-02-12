import os



#REMUS_ADDRESS = '0x02c055a997cc8fc52b1f75ba449db1d8554f7d25ffc6098974ca0754c9693c6d'
#STARKNET_RPC = 'https://starknet-mainnet.public.blastapi.io/rpc/v0_7'
#NETWORK = 'MAINNET'

REMUS_ADDRESS = '0x04e1ac8fe63b465a583740a684aeff3589428d50869b79da2dd296c380ee4bd8'
STARKNET_RPC = 'https://starknet-sepolia.public.blastapi.io/rpc/v0_7'
NETWORK = 'SEPOLIA'

WALLET_ADDRESS = 0x014fac32a2f09fdba71b7af0316fcc276602abcbee9f34c34a6ae936238fff4f

SOURCE_DATA = {
    1: 'https://data-api.binance.vision/api/v3/aggTrades?symbol=ETHUSDC'
    # 2: 'https://data-api.binance.vision/api/v3/aggTrades?symbol=ETHUSDC'
    # 3: 'https://data-api.binance.vision/api/v3/aggTrades?symbol=ETHUSDC'
}

# SOURCE_SPREADS = {
#     1: 'https://data-api.binance.vision/api/v3/bookTicker?symbol=ETHUSDC',
# }

MARKET_MAKER_CFG = {
    1: { # market_id = 1
        'min_distance_from_FP': 0.001,
        'max_error_to_spread': 0.25,
        'order_dollar_size': 10 * 10**18, # in $
        'minimal_remaining_quote_size': 5, # in $
    }
}

MAX_FEE = 200000000000000

DECIMALS = {
    0x49d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7: 18,  # ETH
    0x53c91253bc9682c04929ca02ed00b3e423f6710d2ee7e0d5ebb06f3ecf368a8: 6,  # USDC
    0x4718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d: 18,  # STRK
}
