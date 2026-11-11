import { SessionManager } from "@phantom/cli";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const transaction = Buffer.concat(chunks).toString("utf8").trim();
if (transaction === "") process.exit(2);
const encoded = Buffer.from(transaction, "base64").toString("base64url");
if (encoded === "") process.exit(2);

const manager = new SessionManager({ sessionDir: process.env.DESK_SESSION_DIR });
manager.authenticate = async () => {
  throw new Error("login_refused");
};
await manager.initialize();
const session = manager.getSession();
if (!session || session.walletId !== process.env.DESK_WALLET_ID) process.exit(3);
const client = manager.getClient();
const addresses = await client.getWalletAddresses(session.walletId);
const account = addresses.find((item) => String(item.addressType).toLowerCase() === "solana")?.address;
if (account !== process.env.DESK_WALLET) process.exit(3);
const signed = await client.signTransaction({
  walletId: session.walletId,
  transaction: encoded,
  networkId: "solana:mainnet",
  account,
});
const raw = signed && signed.rawTransaction;
if (typeof raw !== "string" || raw === "") process.exit(4);
let out = raw.replace(/-/g, "+").replace(/_/g, "/");
while (out.length % 4) out += "=";
process.stdout.write(out);
