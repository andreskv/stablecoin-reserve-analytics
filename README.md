# Stablecoin Reserve Integrity: A March 2023 USDC Depeg Study

*A quantitative case study in on-chain event analysis — personal portfolio project*
_Andriy Kovalchuk_

---

Stablecoins are tokenized claims on real-world assets. Their entire value proposition rests on a single assumption: that $1 on-chain is backed by $1 off-chain. When that assumption breaks, the token depegs (when a pegged asset strays significantly from its intended fixed value). From this arises the question of *what the on-chain data showed before it broke* becomes both a regulatory question (Basel, MiCA, SEC) and a practical one for any firm building tokenized financial products.

On **March 10, 2023**, Circle disclosed that $3.3B of USDC's reserves were held at Silicon Valley Bank, which had just been placed under FDIC receivership. USDC fell to **$0.87**. By March 13, the FDIC confirmed full depositor protection and the price recovered. This is the cleanest available stress test a major fiat-backed stablecoin has experienced. We observe a known external trigger, a measurable price response, and full on-chain traceability throughout.

**This is a case study of said event.** Here I build a structured event analysis around the depeg, define a Depeg Pressure Index (DPI) from on-chain and market data, and contrast USDC's behaviour with USDT. The result is a piece of quantitative analysis on a topic that sits at the center of how traditional finance is currently thinking about tokenization.

---

## Hypothesis

> On-chain transfer volume and net exchange inflows are detectable as elevated above their 90-day baseline *before* the price discovery moment, and a composite Depeg Pressure Index combining these signals provides a leading indicator that pure price data does not.

The counter-hypothesis > that price leads and on-chain flows lag as holders react — is equally tested. The findings write-up takes whichever the data actually supports.
