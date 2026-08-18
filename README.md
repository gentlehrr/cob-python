# cob-farm

The official Python SDK for the [COB API](https://cob.farm): upload documents
into isolated silos, get cited answers back.

```
pip install cob-farm
```

```python
import cob

client = cob.Client("cob_sk_live_...")        # or set COB_API_KEY

silo = client.create_silo("deal-room")        # first run: make a silo, save silo.id
# silo = client.get_silo("silo_...")          # later runs: attach to it

receipt = silo.upload(["contract.pdf"], wait=True)   # quoted, charged, ingested
print(receipt)                                        # <cob.Receipt $1.39 for 1 file(s)>

answer = silo.ask("What is the termination notice period?")
print(answer.text)                            # cited: [contract.pdf, Page 12]
```

Uploads are quoted before ingestion and never exceed the quote. If your
balance can't cover a batch, `cob.InsufficientCredits` tells you the exact
shortfall and nothing is charged.

Docs, pricing, and your console: https://cob.farm
