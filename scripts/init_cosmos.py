"""Create the Cosmos database/container used by the bot."""
import os
from azure.cosmos import CosmosClient, PartitionKey
endpoint=os.environ["COSMOS_ENDPOINT"]
key=os.environ["COSMOS_KEY"]
db_name=os.getenv("COSMOS_DATABASE","legal_drafting_bot")
container_name=os.getenv("COSMOS_CONTAINER","cases")
throughput=int(os.getenv("COSMOS_THROUGHPUT","400"))
client=CosmosClient(endpoint,key)
db=client.create_database_if_not_exists(id=db_name)
db.create_container_if_not_exists(id=container_name, partition_key=PartitionKey(path="/user_id"), offer_throughput=throughput)
print(f"Ready: {db_name}/{container_name} partition=/user_id")
