# Exleasingcar Archive

An open-source project for collecting and archiving vehicle prices
and auction results from Exleasingcar.

## Sale types

- open_auction: auctions with visible bidding
- closed_auction: closed auctions
- fixed_price: Buy Now offers

## Important price distinctions

- current_price: currently displayed price
- leading_bid: currently visible leading bid
- own_bid: our account's own bid, never treated as the leading bid
- final_call_price: price captured during Final Call
- closing_bid: last known bid after the auction closes
- confirmed_sale_price: confirmed sale price, only when supported by the source

## Auction and vehicle statuses

Auction status and individual vehicle status must be stored separately.

## Project principles

- Read-only data collection.
- Never place bids or purchase vehicles automatically.
- Preserve historical observations.
- Never claim a sale was confirmed without evidence.
- Respect website terms and request limits.
