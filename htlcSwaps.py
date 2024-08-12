import hashlib
import streamlit as st
import coincurve
from bitcointx.core import CTxOut, CTxIn, COutPoint, CTransaction
from bitcointx.core.script import CScript, OP_DUP, OP_HASH160, OP_EQUALVERIFY, OP_CHECKSIG, OP_IF, OP_ELSE, OP_CHECKLOCKTIMEVERIFY, OP_DROP, OP_ENDIF
from bitcointx.wallet import CCoinAddress
from bitcointx.rpc import Proxy

# Constants
COIN = 100_000_000  # Number of Satoshis in one Bitcoin
DEFAULT_SYMBOL_DIVISIBILITY = 8  # Default divisibility for a new Glyph
DEFAULT_CURRENCY_SYMBOL = "¤"  # Default currency symbol for a new Glyph
MAX_GLYPH_NAME_LENGTH = 26  # Maximum length of a Glyph name, including spacers

class GlyphProtocol:
    """Handles the Glyph protocol logic for Bitcoin transactions with Nostr integration and atomic swaps."""

    BASE_OFFSET = 1  # Set to 1 for 1-26 numbering (Runes), and 0 for 0-25 numbering

    def __init__(self, network='testnet4'):
        """
        Initializes the GlyphProtocol object and selects the Bitcoin network.

        Args:
            network: The Bitcoin network to use ('mainnet' or 'testnet4').
        """
        select_chain_params(network)  # Select the desired Bitcoin network
        self.proxy = Proxy()  # Create a Proxy object for interacting with Bitcoin Core

    def symbol_to_int(self, symbol: str) -> int:
        """
        Converts a Glyph symbol to its integer representation.

        Handles spacers (•) in the Glyph name, ensuring valid placement and length.

        Args:
            symbol: The Glyph symbol (e.g., "TEST•COIN").

        Returns:
            The integer representation of the symbol.

        Raises:
            ValueError: If the symbol contains invalid characters or spacer placement.
        """
        if not self.is_valid_glyph_name(symbol):
            raise ValueError(f"Invalid Glyph name: {symbol}")

        clean_symbol = symbol.replace('•', '')  # Remove spacers for calculation
        value = 0
        for i, c in enumerate(reversed(clean_symbol)):
            value += (ord(c) - ord('A') + self.BASE_OFFSET) * (26 ** i)
        return value

    def int_to_symbol(self, num: int) -> str:
        """
        Converts an integer to its corresponding Glyph symbol.

        Args:
            num: The integer representation of the symbol.

        Returns:
            The Glyph symbol (e.g., "TESTCOIN").

        Raises:
            ValueError: If the input is negative or invalid for the numbering scheme.
        """
        if num < 0:
            raise ValueError("Input must be a non-negative integer")

        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        symbol = ''

        if self.BASE_OFFSET == 1 and num == 0:
            raise ValueError("Input must be a positive integer when using 1-26 numbering")

        if self.BASE_OFFSET == 0 and num == 0:
            return alphabet[0]  # Return 'A' if num is 0

        while num > 0:
            num, remainder = divmod(num - self.BASE_OFFSET, 26)
            symbol = alphabet[remainder] + symbol  

        return symbol

    def encode_varint(self, i: int) -> bytes:
        """
        Encodes an integer as a Bitcoin varint.

        Args:
            i: The integer to encode.

        Returns:
            The varint representation of the integer as bytes.

        Raises:
            ValueError: If the integer is negative.
        """
        if i < 0:
            raise ValueError("Varints cannot be negative")

        encoded = bytes()
        while i > 127:  # 127 is the maximum value encodable in 7 bits
            encoded += bytes([(i & 0x7F) | 0x80])  # Encode lower 7 bits with continuation bit
            i >>= 7  # Shift right by 7 bits for the next iteration
        encoded += bytes([i])  # Encode the last 7 bits
        return encoded

    def decode_varint(self, encoded: bytes) -> int:
        """
        Decodes a Bitcoin varint from bytes.

        Args:
            encoded: The varint bytes to decode.

        Returns:
            The decoded integer.
        """
        decoded = 0
        shift = 0

        for byte in encoded:
            decoded |= (byte & 0x7F) << shift  # Extract 7 bits and accumulate
            if not (byte & 0x80):  # Check for continuation bit
                break
            shift += 7  # Shift for the next byte

        return decoded

    def select_utxo(self, amount_needed_btc: float):
        """
        Selects a suitable UTXO from the wallet to fund a transaction.

        Args:
            amount_needed_btc: The amount of Bitcoin needed in BTC.

        Returns:
            A dictionary representing the selected UTXO with 'txid', 'vout', and 'amount'.

        Raises:
            ValueError: If no suitable UTXO is found.
            JSONRPCError: If there's an error communicating with Bitcoin Core.
        """
        try:
            available_utxos = self.proxy.listunspent()
            for utxo in available_utxos:
                if utxo['amount'] >= amount_needed_btc:
                    utxo['address'] = CCoinAddress(utxo['address'])
                    return utxo
            raise ValueError(f"No UTXO found with sufficient funds (needed: {amount_needed_btc} BTC)")
        except bitcointx.rpc.JSONRPCError as e:
            raise ValueError(f"Error communicating with Bitcoin Core: {str(e)}") 

    def create_htlc_script(self, receiver_pubkey: bytes, sender_pubkey: bytes, 
                           secret_hash: bytes, timelock: int) -> CScript:
        """
        Creates an HTLC script for an atomic swap.

        Args:
            receiver_pubkey: The public key of the recipient.
            sender_pubkey: The public key of the sender.
            secret_hash: The hash of the secret preimage.
            timelock: The block height or Unix timestamp for the time lock.

        Returns:
            The HTLC script.
        """
        return CScript([
            OP_DUP,
            OP_HASH160,
            secret_hash, 
            OP_EQUALVERIFY,
            OP_CHECKSIG,
            OP_IF,
            receiver_pubkey,  # Receiver can claim if they know the secret
            OP_ELSE,
            OP_CHECKLOCKTIMEVERIFY, # Timelock check
            OP_DROP,
            sender_pubkey,  # Sender can claim after timelock
            OP_ENDIF,
            OP_CHECKSIG,
        ])

    def initiate_swap(self, glyph_id: str, amount: int, destination_address: str, 
                      counterparty_pubkey: str, secret: str, timelock: int):
        """
        Initiates an atomic swap by creating an HTLC.

        Args:
            glyph_id: The ID of the Glyph to swap.
            amount: The amount of Glyphs to swap.
            destination_address: The Bitcoin address to receive the Glyphs.
            counterparty_pubkey: The counterparty's public key.
            secret: The secret preimage for the HTLC.
            timelock: The block height or timestamp for the time lock.
        
        Returns:
            The transaction ID of the HTLC.
        """
        secret_hash = hashlib.sha256(secret.encode()).digest()
        receiver_pubkey = coincurve.PublicKey.from_hex(counterparty_pubkey).format(compressed=True)
        sender_pubkey = coincurve.PublicKey.from_hex(self.proxy.getaddressinfo(destination_address)['pubkey']).format(compressed=True)
        
        htlc_script = self.create_htlc_script(receiver_pubkey, sender_pubkey, secret_hash, timelock)

        # Construct and broadcast the HTLC transaction
        txid = self._construct_and_broadcast_transaction(
            CTxOut(int(amount * 10**8), htlc_script),
            change_address=self.proxy.getnewaddress(),
            fee_per_byte=1,
            live=True,  # Broadcast the transaction
        )

        return txid

    def participate_in_swap(self, glyph_id: str, amount: int, counterparty_htlc_details: dict, 
                            destination_address: str):
        """
        Participates in an atomic swap by creating a corresponding HTLC.

        Args:
            glyph_id: The ID of the Glyph to swap.
            amount: The amount of Glyphs to swap.
            counterparty_htlc_details: The details of the counterparty's HTLC.
            destination_address: The Bitcoin address to receive the Glyphs.
        
        Returns:
            The transaction ID of the HTLC.
        """
        secret_hash = bytes.fromhex(counterparty_htlc_details['secret_hash'])
        receiver_pubkey = coincurve.PublicKey.from_hex(counterparty_htlc_details['receiver_pubkey']).format(compressed=True)
        sender_pubkey = coincurve.PublicKey.from_hex(self.proxy.getaddressinfo(destination_address)['pubkey']).format(compressed=True)
        timelock = counterparty_htlc_details['timelock']

        htlc_script = self.create_htlc_script(receiver_pubkey, sender_pubkey, secret_hash, timelock)

        # Construct and broadcast the HTLC transaction
        txid = self._construct_and_broadcast_transaction(
            CTxOut(int(amount * 10**8), htlc_script),
            change_address=self.proxy.getnewaddress(),
            fee_per_byte=1,
            live=True,  # Broadcast the transaction
        )

        return txid

    def claim_glyph(self, htlc_txid: str, secret: str, destination_address: str):
        """
        Claims Glyphs from an HTLC using the preimage.

        Args:
            htlc_txid: The transaction ID of the HTLC.
            secret: The preimage to unlock the HTLC.
            destination_address: The Bitcoin address to receive the Glyphs.

        Returns:
            The transaction ID of the claim transaction.
        """
        # Implementation for claiming the Glyphs from the HTLC
        # This involves creating a transaction that spends the HTLC output
        pass

    def refund_glyph(self, htlc_txid: str, destination_address: str):
        """
        Refunds Glyphs from an expired HTLC.

        Args:
            htlc_txid: The transaction ID of the HTLC.
            destination_address: The Bitcoin address to receive the refunded Glyphs.

        Returns:
            The transaction ID of the refund transaction.
        """
        # Implementation for refunding the Glyphs if the HTLC expires
        # This involves creating a transaction that spends the HTLC output after the timelock
        pass

    def _construct_and_broadcast_transaction(self, glyphstone_output: CTxOut, 
                                             change_address: str = None, 
                                             fee_per_byte: int = 1, live: bool = False,
                                             input_txin: CTxIn = None):
        """
        Constructs, signs, and broadcasts (optional) a Bitcoin transaction.

        Handles transaction construction, fee calculation, change output,
        signing, and broadcasting, including cenotaph handling.

        Args:
            glyphstone_output: CTxOut object containing the Glyphstone data.
            change_address: Address for receiving Bitcoin change.
            fee_per_byte: Transaction fee in satoshis per byte.
            live: Broadcast the transaction if True (default: False).
            input_txin: Optional predefined CTxIn object.

        Returns:
            Transaction ID if broadcasted, otherwise None.

        Raises:
            ValueError: If insufficient funds are available or there are transaction errors.
        """
        # Select an appropriate UTXO from the wallet, or use the provided input
        if not input_txin:
            amount_needed_btc = 0.0001  # Initial estimate, will be adjusted
            utxo = self.select_utxo(amount_needed_btc)
            txin = CTxIn(COutPoint(utxo['txid'], utxo['vout']))
            input_value = utxo['amount'] * COIN 
        else:
            txin = input_txin
            input_value = self.get_input_amount(txin)

        # Assemble transaction outputs
        outputs = [glyphstone_output]

        # Construct the transaction
        tx = CTransaction([txin], outputs)

        # Calculate transaction size and fee
        tx_size = len(tx.serialize())
        fee = tx_size * fee_per_byte

        # Handle change output if necessary
        if change_address:
            change = input_value - fee
            if change > 0:
                change_address_obj = CCoinAddress(change_address)
                change_output = CTxOut(change, change_address_obj.to_scriptPubKey())
                tx.vout.append(change_output)

        # Detect and handle malformed glyphstones (cenotaphs)
        if self.is_cenotaph(glyphstone_output):
            st.warning("Warning: Malformed glyphstone detected. Treating as cenotaph.")
            burn_output = CTxOut(0, CScript([OP_RETURN]))  # Burn input Glyphs
            tx.vout = [burn_output]  # Replace outputs with the burn output

        # Sign and broadcast the transaction if live mode is enabled
        if live:
            try:
                signed_tx = self.proxy.signrawtransactionwithwallet(tx.serialize().hex())
                txid = self.proxy.sendrawtransaction(signed_tx['hex'])
                return txid
            except Exception as e:
                raise ValueError(f"Failed to sign or broadcast transaction: {str(e)}")
        else:
            st.write(tx)
            return None

    def is_cenotaph(self, glyphstone_output: CTxOut) -> bool:
        """
        Checks if a glyphstone output is malformed (a cenotaph).

        Args:
            glyphstone_output: The CTxOut object representing the glyphstone.

        Returns:
            True if the glyphstone is malformed (a cenotaph), False otherwise.
        """
        script = glyphstone_output.scriptPubKey
        return len(script) < 2 or script[0] != OP_RETURN or script[1] != OP_13


def main():
    # Initialize GlyphProtocol
    glyph_protocol = GlyphProtocol()

    st.title("Atomic Glyph Swap Interface")

    # Section 1: Initiate a Swap
    st.header("Initiate a Swap")
    with st.form("initiate_swap"):
        glyph_id = st.text_input("Glyph ID (BLOCK:TX)", "BLOCK:TX")
        amount = st.number_input("Amount", min_value=1, value=1)
        destination_address = st.text_input("Your Bitcoin Address", "your_bitcoin_address")
        counterparty_pubkey = st.text_input("Counterparty's Public Key", "counterparty_pubkey")
        secret = st.text_input("Secret (keep this safe!)", "your_secret")
        
        current_height = glyph_protocol.proxy.getblockcount()
        timelock = current_height + 10

        submitted_initiate = st.form_submit_button("Initiate Swap")
        if submitted_initiate:
            try:
                txid = glyph_protocol.initiate_swap(glyph_id, amount, destination_address, counterparty_pubkey, secret, timelock)
                st.success(f"Swap initiated. Transaction ID: {txid}")

                st.info(f"Provide the following details to your counterparty:\n"
                        f"Glyph ID: {glyph_id}\n"
                        f"Amount: {amount}\n"
                        f"Secret Hash: {hashlib.sha256(secret.encode()).hexdigest()}\n"
                        f"Timelock: {timelock}\n"
                        f"Your Public Key: {glyph_protocol.proxy.getaddressinfo(destination_address)['pubkey']}")
            except Exception as e:
                st.error(f"Error: {str(e)}")

    # Section 2: Participate in a Swap
    st.header("Participate in a Swap")
    with st.form("participate_swap"):
        counterparty_glyph_id = st.text_input("Counterparty's Glyph ID (BLOCK:TX)", "BLOCK:TX")
        participate_amount = st.number_input("Amount to Swap", min_value=1, value=1)
        participate_destination_address = st.text_input("Your Bitcoin Address", "your_bitcoin_address")
        secret_hash = st.text_input("Counterparty's Secret Hash", "counterparty_secret_hash")
        counterparty_pubkey = st.text_input("Counterparty's Public Key", "counterparty_pubkey")
        timelock = st.number_input("Timelock (from Counterparty)", min_value=1, value=timelock)

        submitted_participate = st.form_submit_button("Participate in Swap")
        if submitted_participate:
            try:
                htlc_details = {
                    'secret_hash': secret_hash,
                    'receiver_pubkey': counterparty_pubkey,
                    'timelock': timelock
                }
                txid = glyph_protocol.participate_in_swap(counterparty_glyph_id, participate_amount, htlc_details, participate_destination_address)
                st.success(f"Swap participation successful. Transaction ID: {txid}")
            except Exception as e:
                st.error(f"Error: {str(e)}")

    # Section 3: Claim Glyphs
    st.header("Claim Glyphs")
    with st.form("claim_glyph"):
        claim_htlc_txid = st.text_input("HTLC Transaction ID to Claim", "htlc_txid")
        claim_secret = st.text_input("Secret to Claim", "your_secret")
        claim_destination_address = st.text_input("Your Bitcoin Address", "your_bitcoin_address")

        submitted_claim = st.form_submit_button("Claim Glyphs")
        if submitted_claim:
            try:
                txid = glyph_protocol.claim_glyph(claim_htlc_txid, claim_secret, claim_destination_address)
                st.success(f"Glyphs claimed successfully. Transaction ID: {txid}")
            except Exception as e:
                st.error(f"Error: {str(e)}")

    # Section 4: Refund Glyphs
    st.header("Refund Glyphs")
    with st.form("refund_glyph"):
        refund_htlc_txid = st.text_input("HTLC Transaction ID to Refund", "htlc_txid")
        refund_destination_address = st.text_input("Your Bitcoin Address", "your_bitcoin_address")

        submitted_refund = st.form_submit_button("Refund Glyphs")
        if submitted_refund:
            try:
                txid = glyph_protocol.refund_glyph(refund_htlc_txid, refund_destination_address)
                st.success(f"Glyphs refunded successfully. Transaction ID: {txid}")
            except Exception as e:
                st.error(f"Error: {str(e)}")


if __name__ == '__main__':
    main()
