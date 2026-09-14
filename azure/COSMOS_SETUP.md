# Azure Cosmos DB setup for Legal Drafting Bot

## 1. Create the account

Azure Portal → Create a resource → Azure Cosmos DB → Azure Cosmos DB for NoSQL.

Recommended:
- API: NoSQL
- Region: closest practical region to your users
- Capacity: Provisioned throughput
- Enable Azure Cosmos DB free tier if available and unused by another Cosmos account.
- Keep throughput small (400 RU/s is enough for this bot's low-volume case-state workload).

Azure for Students currently advertises Cosmos DB free amounts and USD 100 credit; check the portal before deployment because eligibility/allowances can change.

## 2. Create the database/container

Database: `legal_drafting_bot`
Container: `cases`
Partition key: `/user_id`

Do NOT upload the 306 legal drafts into Cosmos. Keep the corpus/index in the repo or a dedicated retrieval store.

## 3. Get credentials

Cosmos account → Keys → copy the URI and Primary Key. Never commit the primary key to GitHub.

## 4. Environment variables

`COSMOS_ENDPOINT=https://YOUR-ACCOUNT.documents.azure.com:443/`
`COSMOS_KEY=YOUR_PRIMARY_KEY`
`COSMOS_DATABASE=legal_drafting_bot`
`COSMOS_CONTAINER=cases`
`COSMOS_THROUGHPUT=400`

If COSMOS_ENDPOINT/COSMOS_KEY are missing, the bot automatically falls back to local SQLite.
