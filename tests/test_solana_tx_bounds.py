"""Untrusted transaction bytes must reject cleanly before any RPC access."""

import base64
import unittest

import solana_inspect
import solana_tx


WALLET = solana_tx.b58encode(bytes([7]) * 32)
DESTINATION = solana_tx.b58encode(bytes([8]) * 32)
BLOCKHASH = solana_tx.b58encode(bytes([9]) * 32)
TABLE = solana_tx.b58encode(bytes([10]) * 32)


def legacy():
    return base64.b64decode(solana_tx.encode_legacy_transfer(
        WALLET, DESTINATION, 10_000_000, BLOCKHASH,
    ))


def versioned():
    return base64.b64decode(solana_tx.encode_v0_transfer_lookup(
        WALLET, TABLE, 10_000_000, BLOCKHASH,
    ))


class TestSolanaTransactionBounds(unittest.TestCase):
    def test_valid_legacy_and_versioned_transactions_still_decode(self):
        for raw, version in [(legacy(), "legacy"), (versioned(), "v0")]:
            with self.subTest(version=version):
                decoded = solana_tx.decode_transaction(raw)
                self.assertEqual(decoded.version, version)
                self.assertEqual(decoded.signers, [WALLET])
                self.assertEqual(decoded.blockhash, BLOCKHASH)
                self.assertEqual(len(decoded.instructions), 1)
        self.assertEqual(solana_tx.decode_transaction(versioned()).lookups[0].table, TABLE)

    def test_writable_permissions_follow_static_header_and_lookup_order(self):
        decoded = solana_tx.decode_transaction(legacy())
        self.assertEqual(decoded.readonly_signed, 0)
        self.assertEqual(decoded.readonly_unsigned, 1)
        self.assertEqual([decoded.is_writable(i) for i in range(4)], [True, True, False, False])
        decoded = solana_tx.decode_transaction(versioned())
        self.assertEqual([decoded.is_writable(i) for i in range(4)], [True, False, True, False])
        self.assertFalse(decoded.is_writable(-1))
        self.assertFalse(decoded.is_writable(True))
        self.assertFalse(decoded.is_writable("0"))

        # Lookups concatenate every table's writables, then every readonly.
        decoded.lookups = [
            solana_tx.Lookup(TABLE, [0], [1, 2]),
            solana_tx.Lookup(TABLE, [3], [4]),
        ]
        self.assertEqual(
            [decoded.is_writable(i) for i in range(2, 8)],
            [True, True, False, False, False, False],
        )

    def test_readonly_signer_permissions_are_preserved(self):
        raw = bytearray(base64.b64decode(solana_tx.encode_legacy(
            WALLET, [WALLET, DESTINATION, solana_tx.SYSTEM_PROGRAM], 1,
            [(2, [0, 1], (2).to_bytes(4, "little") + (1).to_bytes(8, "little"))],
            BLOCKHASH, required=2,
        )))
        raw[1 + 128 + 1] = 1
        decoded = solana_tx.decode_transaction(bytes(raw))
        self.assertEqual(decoded.readonly_signed, 1)
        self.assertEqual([decoded.is_writable(i) for i in range(3)], [True, False, False])

    def test_every_truncated_prefix_is_a_value_error(self):
        for raw in (legacy(), versioned()):
            for length in range(len(raw)):
                with self.subTest(versioned=raw[65] == 0x80, length=length):
                    with self.assertRaises(ValueError):
                        solana_tx.decode_transaction(raw[:length])

    def test_missing_instruction_byte_is_rejected_without_rpc(self):
        # This previously raised an uncaught IndexError at program_index.
        raw = bytes([1]) + bytes(64)
        raw += bytes([1, 0, 0, 1]) + bytes(32) + bytes(32) + bytes([1])

        def no_rpc(_payload):
            self.fail("malformed transaction must not reach an RPC request")

        result = solana_inspect.inspect(
            base64.b64encode(raw).decode(), solana_inspect.RpcClient(no_rpc),
        )
        self.assertEqual(result["error_category"], "not_usable")
        self.assertFalse(result["chain_ok"])
        self.assertFalse(result["execution_allowed"])

    def test_signature_count_and_message_header_must_agree(self):
        raw = bytes([2]) + bytes(128) + legacy()[65:]
        with self.assertRaises(ValueError):
            solana_tx.decode_transaction(raw)

    def test_readonly_counts_cannot_include_payer_or_exceed_keys(self):
        for index, value in [(66, 1), (67, 3)]:
            raw = bytearray(legacy())
            raw[index] = value
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    solana_tx.decode_transaction(bytes(raw))

    def test_noncanonical_and_overflow_short_vectors_are_rejected(self):
        raw = legacy()
        # Account-key count follows one signature and the three-byte header.
        for encoded in [b"\x83\x00", b"\x83\x80\x00", b"\xff\xff\x04"]:
            with self.subTest(encoded=encoded):
                with self.assertRaises(ValueError):
                    solana_tx.decode_transaction(raw[:68] + encoded + raw[69:])
        # The same rule applies to the outer signature vector.
        with self.assertRaises(ValueError):
            solana_tx.decode_transaction(b"\x81\x00" + raw[1:])

    def test_program_and_account_indexes_must_resolve(self):
        # Header, three keys, blockhash, instruction count precede the program.
        program_offset = 65 + 4 + 3 * 32 + 32 + 1
        for offset in (program_offset, program_offset + 2):
            raw = bytearray(legacy())
            raw[offset] = 3
            with self.subTest(offset=offset):
                with self.assertRaises(ValueError):
                    solana_tx.decode_transaction(bytes(raw))

    def test_trailing_bytes_and_unsupported_version_are_rejected(self):
        for raw in (legacy() + b"\0", versioned() + b"\0"):
            with self.assertRaises(ValueError):
                solana_tx.decode_transaction(raw)
        raw = bytearray(versioned())
        raw[65] = 0x81
        with self.assertRaises(ValueError):
            solana_tx.decode_transaction(bytes(raw))


if __name__ == "__main__":
    unittest.main()
