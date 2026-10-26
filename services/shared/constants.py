# Event names used as routing keys in RabbitMQ.
EVENT_ORDER_PLACED = "order.placed"
EVENT_ORDER_CONFIRMED = "order.confirmed"
EVENT_STOCK_ALLOCATED = "stock.allocated"
EVENT_STOCK_FAILED = "stock.failed"

# RabbitMQ Exchange configuration.
EXCHANGE_NAME = "store_io_exchange"
EXCHANGE_TYPE = "topic"
