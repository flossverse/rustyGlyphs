import argparse
import json
import bitcointx.rpc
from bitcointx.core import b2lx, COutPoint, CTransaction, CTxIn, CTxOut, CScript
from bitcointx.core.script import OP_RETURN, OP_13, OP_0, OP_1
from bitcointx.wallet import CCoinAddress, P2TRCoinAddress
from bitcointx import select_chain_params
import unicodedata
import hashlib
import coincurve

# Constants
COIN = 100_000_000  # Number of Satoshis in one Bitcoin
DEFAULT_SYMBOL_DIVISIBILITY = 8  # Default divisibility for a new Glyph
DEFAULT_CURRENCY_SYMBOL = "¤"  # Default currency symbol for a new Glyph
MAX_GLYPH_NAME_LENGTH = 26  # Maximum length of a Glyph name, including spacers

class GlyphProtocol:
    """Handles the Glyph protocol logic for Bitcoin transactions with Nostr integration."""

    BASE_OFFSET = 1  # Set to 1 for 1-26 numbering (Runes), and 0 for 0-25 numbering

    def __init__(self, network='testnet4'):
        """
        Initializes the GlyphProtocol object and selects the Bitcoin network.

        Args:
            network: The Bitcoin network to use ('mainnet' or 'testnet4').
        """
        select_chain_params(network)  # Select the desired Bitcoin network
        self.proxy = bitcointx.rpc.Proxy()  # Create a Proxy object for interacting with Bitcoin Core

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
            bitcointx.rpc.JSONRPCError: If there's an error communicating with Bitcoin Core.
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

    def create_glyphstone_output(self, glyphstone_data: bytes) -> CTxOut:
        """
        Creates a CTxOut object for a Glyphstone OP_RETURN output.

        Args:
            glyphstone_data: The encoded Glyphstone data.

        Returns:
            A CTxOut object for the Glyphstone output.
        """
        return CTxOut(0, CScript([OP_RETURN, OP_13, glyphstone_data])) 

    def etch_glyph(self, name: str, divisibility: int = DEFAULT_SYMBOL_DIVISIBILITY, 
                   symbol: str = DEFAULT_CURRENCY_SYMBOL, premine: int = 0, 
                   mint_cap: int = None, mint_amount: int = None, start_height: int = None, 
                   end_height: int = None, start_offset: int = None, end_offset: int = None, 
                   destination_address: str = None, change_address: str = None, 
                   fee_per_byte: int = 1, live: bool = False, nostr_pubkey: str = None):
        """
        Etches a new Glyph on the Bitcoin blockchain with optional Nostr integration.

        This method constructs the Glyphstone data, selects a suitable UTXO, 
        creates the transaction outputs (including Taproot address if needed), 
        and broadcasts the transaction if `live` is set to True.

        Args:
            name: The name of the Glyph (e.g., "TEST•COIN").
            divisibility: The number of decimal places (default: 8).
            symbol: The currency symbol (default: "¤").
            premine: Initial amount of Glyphs allocated to the etcher (default: 0).
            mint_cap: Optional cap on the total number of mints allowed.
            mint_amount: Optional fixed amount of Glyphs minted per transaction.
            start_height: Optional block height at which the open mint begins.
            end_height: Optional block height at which the open mint ends.
            start_offset: Optional block offset from the etch block to start the open mint.
            end_offset: Optional block offset from the etch block to end the open mint.
            destination_address: Address to receive the premined Glyphs (if any).
            change_address: Address to receive any remaining Bitcoin change.
            fee_per_byte: Transaction fee in satoshis per byte (default: 1).
            live: Broadcast the transaction to the network if True (default: False).
            nostr_pubkey: Optional Nostr public key for Taproot integration.

        Returns:
            The transaction ID if the transaction is broadcast, otherwise None.

        Raises:
            ValueError: If the Glyph name is invalid, or if a premine is attempted 
                        without a destination address.
        """
        # Validate and encode inputs
        name_int = self.symbol_to_int(name)
        glyphstone_data = b'E' + self.encode_varint(name_int) + self.encode_varint(divisibility)

        # Add optional minting parameters to the glyphstone data
        glyphstone_data = self._add_optional_mint_params(
            glyphstone_data, symbol, premine, mint_cap, mint_amount,
            start_height, end_height, start_offset, end_offset
        )

        # Create the Glyphstone output
        glyphstone_output = self.create_glyphstone_output(glyphstone_data)

        # Create the output for premined Glyphs (if any)
        destination_output = self._create_glyph_output(
            premine, divisibility, destination_address, nostr_pubkey
        )

        # Construct and broadcast the transaction
        return self._construct_and_broadcast_transaction(
            glyphstone_output, destination_output, change_address, fee_per_byte, live
        )


    def mint_glyph(self, glyph_id: str, amount: int, destination_address: str, 
                   change_address: str = None, fee_per_byte: int = 1, live: bool = False,
                   nostr_pubkey: str = None):
        """
        Mints new units of an existing Glyph, ensuring mint conditions are met.

        This method checks the mint terms, constructs the Glyphstone data, 
        creates the output for minted Glyphs, and broadcasts the transaction.

        Args:
            glyph_id: The ID of the Glyph to mint in "BLOCK:TX" format.
            amount: The amount of Glyphs to mint.
            destination_address: The address to receive the minted Glyphs.
            change_address: The address to receive any leftover Bitcoin.
            fee_per_byte: The transaction fee per byte (default: 1 satoshi).
            live: If True, the transaction will be broadcast to the network (default: False).
            nostr_pubkey: Optional Nostr public key to integrate via Taproot.

        Returns:
            The transaction ID if the transaction is broadcast, otherwise None.

        Raises:
            ValueError: If the mint is closed or minting terms are violated.
        """

        # Get Glyph information and check if minting is currently allowed
        glyph_info = self.get_glyph_info(glyph_id)
        current_height = self.proxy.getblockcount()
        if not self.is_mint_open(glyph_info, current_height):
            raise ValueError(f"Mint is closed for Glyph {glyph_id}")

        if glyph_info['mint_amount'] and amount != glyph_info['mint_amount']:
            raise ValueError(f"Invalid mint amount. Must be {glyph_info['mint_amount']}")

        # Construct the Glyphstone data for minting
        block_height, tx_index = map(int, glyph_id.split(':'))
        glyphstone_data = b'M' + self.encode_varint(block_height) + self.encode_varint(tx_index) + self.encode_varint(amount)

        # Create the Glyphstone output
        glyphstone_output = self.create_glyphstone_output(glyphstone_data)

        # Create the output for the minted Glyphs
        output_value = int(amount * (10**glyph_info['divisibility']))  # Convert to atomic units
        destination_address_obj = self.create_taproot_address(destination_address, nostr_pubkey) if nostr_pubkey else CCoinAddress(destination_address)
        destination_output = CTxOut(output_value, destination_address_obj.to_scriptPubKey())

        # Construct and broadcast the transaction
        return self._construct_and_broadcast_transaction(
            glyphstone_output, destination_output, change_address, fee_per_byte, live
        )

    def transfer_glyph(self, glyph_id: str, input_txid: str, input_vout: int, amount: int, 
                       destination_address: str, change_address: str = None, 
                       fee_per_byte: int = 1, live: bool = False, nostr_pubkey: str = None):
        """
        Transfers Glyphs from one address to another with optional burning.

        This method constructs the Glyphstone data, retrieves the input amount,
        creates outputs (including a burn output if needed), and broadcasts the transaction.

        Args:
            glyph_id: The ID of the Glyph to transfer in "BLOCK:TX" format.
            input_txid: The transaction ID of the UTXO containing the Glyphs.
            input_vout: The output index of the UTXO containing the Glyphs.
            amount: The amount of Glyphs to transfer.
            destination_address: The address to receive the transferred Glyphs 
                                 (or 'OP_RETURN' to burn).
            change_address: The address to receive any leftover Bitcoin and Glyphs.
            fee_per_byte: The transaction fee per byte (default: 1 satoshi).
            live: If True, the transaction will be broadcast to the network (default: False).
            nostr_pubkey: Optional Nostr public key for Taproot integration.

        Returns:
            The transaction ID if the transaction is broadcast, otherwise None.

        Raises:
            ValueError: If the input does not contain sufficient Glyphs.
        """
        # Input validation and decoding
        block_height, tx_index = map(int, glyph_id.split(':'))

        # Ensure sufficient Glyphs are available in the input
        input_glyphs = self.get_glyph_balance(input_txid, input_vout, glyph_id)
        if input_glyphs < amount:
            raise ValueError(f"Insufficient Glyphs in input. Available: {input_glyphs}, Requested: {amount}")

        # Construct the Glyphstone data for the transfer
        glyphstone_data = (
            b'T'
            + self.encode_varint(block_height)
            + self.encode_varint(tx_index)
            + self.encode_varint(amount)
            + self.encode_varint(1)  # Output index 1 for the destination output
        )

        # Create the OP_RETURN output for the Glyphstone
        glyphstone_output = self.create_glyphstone_output(glyphstone_data)

        # Create the transaction input
        txin = CTxIn(COutPoint(bytes.fromhex(input_txid)[::-1], input_vout))

        # Create the output for the transferred Glyphs or a burn output
        if destination_address.startswith('OP_RETURN'):
            destination_output = CTxOut(0, CScript([OP_RETURN]))
        else:
            destination_address_obj = self.create_taproot_address(destination_address, nostr_pubkey) if nostr_pubkey else CCoinAddress(destination_address)
            destination_output = CTxOut(amount, destination_address_obj.to_scriptPubKey())

        # Construct and broadcast the transaction
        return self._construct_and_broadcast_transaction(
            glyphstone_output, destination_output, change_address, fee_per_byte, live, txin
        )

    def is_valid_glyph_name(self, name: str) -> bool:
        """
        Validates a Glyph name.

        Rules:
        1. Only uppercase letters A-Z and spacers (•) are allowed.
        2. Spacers can only be placed between two letters.
        3. The total length (including spacers) must not exceed MAX_GLYPH_NAME_LENGTH.
        4. The name must contain at least one letter.

        Args:
            name: The Glyph name to validate.

        Returns:
            True if the name is valid, False otherwise.
        """
        if not name or len(name) > MAX_GLYPH_NAME_LENGTH:
            return False

        letter_count = 0
        for i, char in enumerate(name):
            if char.isalpha() and char.isupper():
                letter_count += 1
            elif char == '•':
                # Check for valid spacer placement: not at the start, end, or consecutive spacers
                if i == 0 or i == len(name) - 1 or name[i-1] == '•' or name[i+1] == '•':
                    return False
            else:
                return False

        return letter_count > 0 and letter_count <= MAX_GLYPH_NAME_LENGTH

    def is_valid_currency_symbol(self, symbol: str) -> bool:
        """
        Validates a currency symbol for a Glyph.

        Rules:
        1. The symbol must be a single Unicode code point.
        2. The symbol must not be a letter or number.

        Args:
            symbol: The currency symbol to validate.

        Returns:
            True if the symbol is valid, False otherwise.
        """
        if len(symbol) != 1:
            return False

        category = unicodedata.category(symbol)
        return not (category.startswith('L') or category.startswith('N'))

    def is_mint_open(self, glyph_info: dict, current_height: int) -> bool:
        """
        Checks if minting is allowed for a given Glyph.

        Verifies if the current block height is within the specified minting range
        and if the mint cap has not been reached.

        Args:
            glyph_info: A dictionary containing the Glyph's minting information.
            current_height: The current block height.

        Returns:
            True if minting is open, False otherwise.
        """
        if glyph_info.get('mint_cap') and glyph_info.get('minted_count') >= glyph_info['mint_cap']:
            return False

        etch_height = glyph_info.get('etch_height', 0)  # Get etch height, default to 0 if not found
        start_height = glyph_info.get('start_height') or (etch_height + glyph_info.get('start_offset', 0) if glyph_info.get('start_offset') else 0)
        end_height = glyph_info.get('end_height') or (etch_height + glyph_info.get('end_offset', 0) if glyph_info.get('end_offset') else float('inf'))

        return start_height <= current_height < end_height

def get_glyph_info(self, glyph_id: str) -> dict:
    """
    Retrieves information about a Glyph from the blockchain.

    Args:
        glyph_id: The ID of the Glyph in "BLOCK:TX" format.

    Returns:
        A dictionary containing the Glyph's information and minting terms.

    Raises:
        ValueError: If the glyph_id is invalid or the Glyph is not found.
    """
    try:
        block_height, tx_index = map(int, glyph_id.split(':'))
    except ValueError:
        raise ValueError(f"Invalid glyph_id format: {glyph_id}")

    # Retrieve the block containing the Glyph etch transaction
    block_hash = self.proxy.getblockhash(block_height)
    block = self.proxy.getblock(block_hash, 2)  # 2 for verbose mode with transaction details

    if tx_index >= len(block['tx']):
        raise ValueError(f"Transaction index {tx_index} out of range for block {block_height}")

    transaction = block['tx'][tx_index]

    # Parse the transaction outputs to find the Glyphstone data
    glyphstone_data = None
    for output in transaction['vout']:
        scriptPubKey = output['scriptPubKey']
        if scriptPubKey['type'] == 'nulldata' and len(scriptPubKey['asm'].split()) > 2:
            asm_parts = scriptPubKey['asm'].split()
            if asm_parts[1] == 'OP_13':
                glyphstone_data = bytes.fromhex(asm_parts[2])
                break

    if not glyphstone_data:
        raise ValueError(f"Glyph not found with ID: {glyph_id}")

    # Decode the Glyphstone data
    return self.decode_glyphstone(glyphstone_data, block_height)

def decode_glyphstone(self, glyphstone_data: bytes, etch_height: int) -> dict:
    """
    Decodes Glyphstone data into a dictionary of Glyph properties.

    Args:
        glyphstone_data: The raw Glyphstone data.
        etch_height: The block height where the Glyph was etched.

    Returns:
        A dictionary containing the Glyph's information and minting terms.
    """
    glyph_info = {
        'etch_height': etch_height,
        'minted_count': 0  # This should be updated based on actual minting data
    }

    if glyphstone_data[0] != ord('E'):
        raise ValueError("Invalid Glyphstone data: doesn't start with 'E'")

    data = glyphstone_data[1:]
    name_int, data = self.decode_varint(data)
    glyph_info['name'] = self.int_to_symbol(name_int)

    divisibility, data = self.decode_varint(data)
    glyph_info['divisibility'] = divisibility

    if data:
        glyph_info['symbol'] = data[0:1].decode('utf-8')
        data = data[1:]

    while data:
        if data[0] == ord('C'):
            mint_cap, data = self.decode_varint(data[1:])
            glyph_info['mint_cap'] = mint_cap
        elif data[0] == ord('A'):
            mint_amount, data = self.decode_varint(data[1:])
            glyph_info['mint_amount'] = mint_amount
        elif data[0] == ord('S'):
            start_height, data = self.decode_varint(data[1:])
            glyph_info['start_height'] = start_height
        elif data[0] == ord('H'):
            end_height, data = self.decode_varint(data[1:])
            glyph_info['end_height'] = end_height
        elif data[0] == ord('O'):
            start_offset, data = self.decode_varint(data[1:])
            glyph_info['start_offset'] = start_offset
        elif data[0] == ord('F'):
            end_offset, data = self.decode_varint(data[1:])
            glyph_info['end_offset'] = end_offset
        else:
            break  # Unknown field, stop parsing

    return glyph_info


def get_glyph_balance(self, txid: str, vout: int, glyph_id: str) -> int:
    """
    Retrieves the Glyph balance from a specific UTXO.

    Args:
        txid: The transaction ID of the UTXO.
        vout: The output index of the UTXO.
        glyph_id: The ID of the Glyph in "BLOCK:TX" format.

    Returns:
        The balance of the specified Glyph in the UTXO.

    Raises:
        ValueError: If the UTXO does not exist or does not contain the specified Glyph.
    """
    try:
        # Retrieve the raw transaction
        raw_tx = self.proxy.getrawtransaction(txid, True)
        
        if vout >= len(raw_tx['vout']):
            raise ValueError(f"Output index {vout} out of range for transaction {txid}")

        output = raw_tx['vout'][vout]
        
        # Check if the output is unspent
        try:
            self.proxy.gettxout(txid, vout)
        except bitcointx.rpc.JSONRPCError:
            raise ValueError(f"UTXO {txid}:{vout} has been spent")

        # Parse the scriptPubKey to find the Glyphstone data
        scriptPubKey = output['scriptPubKey']
        if scriptPubKey['type'] != 'nulldata':
            raise ValueError(f"UTXO {txid}:{vout} does not contain Glyphstone data")

        asm_parts = scriptPubKey['asm'].split()
        if len(asm_parts) < 3 or asm_parts[1] != 'OP_13':
            raise ValueError(f"UTXO {txid}:{vout} does not contain valid Glyphstone data")

        glyphstone_data = bytes.fromhex(asm_parts[2])
        
        # Decode the Glyphstone data to get the Glyph balance
        glyph_balance = self.decode_glyph_balance(glyphstone_data, glyph_id)
        
        return glyph_balance

    except bitcointx.rpc.JSONRPCError as e:
        raise ValueError(f"Error retrieving UTXO data: {str(e)}")
    
def get_input_amount(self, txin: CTxIn) -> int:
    """
    Retrieves the amount of Bitcoin in satoshis from a given transaction input.

    Args:
        txin: The CTxIn object representing the input.

    Returns:
        The amount of Bitcoin in the input, in satoshis.
    """
    txid = txin.prevout.hash.hex()
    transaction = self.proxy.getrawtransaction(txid)
    output = transaction.vout[txin.prevout.n]
    return int(output.nValue)

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


def decode_glyph_balance(self, glyphstone_data: bytes, glyph_id: str) -> int:
    """
    Decodes the Glyph balance from Glyphstone data.

    Args:
        glyphstone_data: The raw Glyphstone data.
        glyph_id: The ID of the Glyph to check for.

    Returns:
        The balance of the specified Glyph.

    Raises:
        ValueError: If the Glyphstone data is invalid or doesn't contain the specified Glyph.
    """
    if glyphstone_data[0] != ord('T'):
        raise ValueError("Invalid Glyphstone data: doesn't start with 'T'")

    data = glyphstone_data[1:]
    block_height, data = self.decode_varint(data)
    tx_index, data = self.decode_varint(data)
    
    if f"{block_height}:{tx_index}" != glyph_id:
        raise ValueError(f"Glyphstone does not contain Glyph with ID {glyph_id}")

    balance, _ = self.decode_varint(data)
    return balance


    def _construct_and_broadcast_transaction(self, glyphstone_output: CTxOut, 
                                             destination_output: CTxOut = None,
                                             change_address: str = None, 
                                             fee_per_byte: int = 1, live: bool = False,
                                             input_txin: CTxIn = None):
        """
        Constructs, signs, and broadcasts (optional) a Bitcoin transaction.

        Handles transaction construction, fee calculation, change output,
        signing, and broadcasting, including cenotaph handling.

        Args:
            glyphstone_output: CTxOut object containing the Glyphstone data.
            destination_output: Optional CTxOut object for the recipient.
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
        if destination_output:
            outputs.append(destination_output)

        # Construct the transaction
        tx = CTransaction([txin], outputs)

        # Calculate transaction size and fee
        tx_size = len(tx.serialize())
        fee = tx_size * fee_per_byte

        # Handle change output if necessary
        if change_address:
            change = input_value - (destination_output.nValue if destination_output else 0) - fee
            if change > 0:
                change_address_obj = CCoinAddress(change_address)
                change_output = CTxOut(change, change_address_obj.to_scriptPubKey())
                tx.vout.append(change_output)

        # Detect and handle malformed glyphstones (cenotaphs)
        if self.is_cenotaph(glyphstone_output):
            print("Warning: Malformed glyphstone detected. Treating as cenotaph.")
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
            print(tx)
            return None

    def create_taproot_address(self, bitcoin_address: str, nostr_pubkey: str) -> P2TRCoinAddress:
        """
        Creates a Taproot address integrating a Bitcoin address and a Nostr public key.

        Constructs a Taproot address by combining the script pubkey of a standard Bitcoin address
        with a leaf that commits to the Nostr public key, enhancing security and functionality.

        Args:
            bitcoin_address: The standard Bitcoin address.
            nostr_pubkey: The Nostr public key for integration.

        Returns:
            A P2TRCoinAddress object representing the Taproot address.
        """
        addr = CCoinAddress(bitcoin_address)
        script_pubkey = addr.to_scriptPubKey()
        nostr_leaf = CScript([OP_1, bytes.fromhex(nostr_pubkey)])
        taproot_script = CScript([OP_1, script_pubkey.to_bytes(), nostr_leaf.to_bytes()])
        taproot_address = P2TRCoinAddress.from_scriptPubKey(taproot_script)
        return taproot_address

    def _add_optional_mint_params(self, glyphstone_data: bytes, symbol: str, premine: int, 
                              mint_cap: int, mint_amount: int, start_height: int, 
                              end_height: int, start_offset: int, end_offset: int) -> bytes:
        """
        Adds optional minting parameters to the Glyphstone data.

        Encodes optional minting parameters as defined in the Glyphs protocol 
        and appends them to the existing glyphstone data.

        Args:
            glyphstone_data: The initial Glyphstone data (bytes).
            symbol: The currency symbol (string).
            premine: The amount of premined Glyphs (integer).
            mint_cap: The cap on the number of mints (integer).
            mint_amount: The amount of Glyphs per mint (integer).
            start_height: The starting block height for minting (integer).
            end_height: The ending block height for minting (integer).
            start_offset: The starting block offset for minting (integer).
            end_offset: The ending block offset for minting (integer).

        Returns:
            The glyphstone data with the optional parameters appended (bytes).
        """
        if symbol:
            glyphstone_data += symbol.encode('utf-8')
        if premine:
            glyphstone_data += self.encode_varint(premine)
        if mint_cap:
            glyphstone_data += b'C' + self.encode_varint(mint_cap)
        if mint_amount:
            glyphstone_data += b'A' + self.encode_varint(mint_amount)
        if start_height:
            glyphstone_data += b'S' + self.encode_varint(start_height)
        if end_height:
            glyphstone_data += b'H' + self.encode_varint(end_height)
        if start_offset:
            glyphstone_data += b'O' + self.encode_varint(start_offset)
        if end_offset:
            glyphstone_data += b'F' + self.encode_varint(end_offset)
        return glyphstone_data

def _create_glyph_output(self, amount: int, divisibility: int, 
                           destination_address: str, nostr_pubkey: str = None) -> CTxOut:
    """
    Creates a CTxOut object for Glyphs, optionally using Taproot.

    Constructs a CTxOut object for the given amount of Glyphs, 
    converting them to atomic units based on divisibility. If a Nostr 
    public key is provided, a Taproot address is used.

    Args:
        amount: The amount of Glyphs to send.
        divisibility: The number of decimal places for the Glyph.
        destination_address: The Bitcoin address to receive the Glyphs.
        nostr_pubkey: Optional Nostr public key for Taproot integration.

    Returns:
        A CTxOut object for the Glyphs.

    Raises:
        ValueError: If amount is non-zero and destination_address is not provided.
    """
    if amount > 0:
        if not destination_address:
            raise ValueError("Destination address is required for a non-zero amount of Glyphs")
        destination_address_obj = self.create_taproot_address(destination_address, nostr_pubkey) if nostr_pubkey else CCoinAddress(destination_address)
        output_value = int(amount * (10**divisibility))
        return CTxOut(output_value, destination_address_obj.to_scriptPubKey())
    return None
def main(): """Parses command-line arguments and interacts with the GlyphProtocol.""" parser = argparse.ArgumentParser(description='Glyphs Command Line Interface.')
subparsers = parser.add_subparsers(dest='command', help='Subcommand to run')
# --- Issue Command ---
issue_parser = subparsers.add_parser('issue', help='Issue a new Glyph.')
issue_parser.add_argument('name', type=str, help='Name of the Glyph to be issued (e.g., "TESTCOIN").')
issue_parser.add_argument('--divisibility', type=int, default=DEFAULT_SYMBOL_DIVISIBILITY, 
                          help='Number of decimal places for the Glyph (default: 8).')
issue_parser.add_argument('--symbol', type=str, default=DEFAULT_CURRENCY_SYMBOL, 
                          help='Currency symbol for the Glyph (default: "¤").')
issue_parser.add_argument('--premine', type=int, default=0, 
                          help='Amount of Glyphs to premine to the destination address (default: 0).')
issue_parser.add_argument('--mint_cap', type=int, help='Optional cap on the number of mints allowed.')
issue_parser.add_argument('--mint_amount', type=int, 
                          help='Optional fixed amount of Glyphs to be minted per transaction.')
issue_parser.add_argument('--start_height', type=int, help='Optional block height to start the open mint.')
issue_parser.add_argument('--end_height', type=int, help='Optional block height to end the open mint.')
issue_parser.add_argument('--start_offset', type=int, 
                          help='Optional block offset from the etch block to start the open mint.')
issue_parser.add_argument('--end_offset', type=int, 
                          help='Optional block offset from the etch block to end the open mint.')
issue_parser.add_argument('--destination_address', type=str, 
                          help='Destination address for premined Glyphs (required if premine > 0).')
issue_parser.add_argument('--change_address', type=str, help='Change address for Bitcoin.')
issue_parser.add_argument('--fee', type=int, default=1, help='Transaction fee in satoshis per byte (default: 1).')
issue_parser.add_argument('--live', action='store_true', help='Broadcast the transaction to the network.')
issue_parser.add_argument('--nostr_pubkey', type=str, help='Optional Nostr public key to integrate via Taproot.')
# --- Mint Command ---
mint_parser = subparsers.add_parser('mint', help='Mint new units of a Glyph.')
mint_parser.add_argument('glyph_id', type=str, help='Glyph ID to mint in "BLOCK:TX" format.')
mint_parser.add_argument('amount', type=int, help='Amount of Glyphs to mint.')
mint_parser.add_argument('destination_address', type=str, help='Destination address for the minted Glyphs.')
mint_parser.add_argument('--change_address', type=str, help='Change address for Bitcoin.')
mint_parser.add_argument('--fee', type=int, default=1, help='Transaction fee in satoshis per byte (default: 1).')
mint_parser.add_argument('--live', action='store_true', help='Broadcast the transaction to the network.')
mint_parser.add_argument('--nostr_pubkey', type=str, help='Optional Nostr public key to integrate via Taproot.')

# --- Transfer Command ---
transfer_parser = subparsers.add_parser('transfer', help='Transfer Glyphs.')
transfer_parser.add_argument('glyph_id', type=str, help='Glyph ID to transfer in "BLOCK:TX" format.')
transfer_parser.add_argument('input_txid', type=str, help='Transaction ID of the input UTXO.')
transfer_parser.add_argument('input_vout', type=int, help='Output index of the input UTXO.')
transfer_parser.add_argument('amount', type=int, help='Amount of Glyphs to transfer.')
transfer_parser.add_argument('destination_address', type=str, help='Destination address for the Glyphs.')
transfer_parser.add_argument('--change_address', type=str, help='Change address for Bitcoin and remaining Glyphs.')
transfer_parser.add_argument('--fee', type=int, default=1, help='Transaction fee in satoshis per byte (default: 1).')
transfer_parser.add_argument('--live', action='store_true', help='Broadcast the transaction to the network.')
transfer_parser.add_argument('--nostr_pubkey', type=str, help='Optional Nostr public key to integrate via Taproot.')
# --- Symbol Encoding/Decoding ---
symbol_parser = subparsers.add_parser('symbol', help='Encode/Decode a Glyph symbol.')
symbol_parser.add_argument('action', type=str, choices=['encode', 'decode'], 
                           help='Whether to encode or decode the symbol.')
symbol_parser.add_argument('value', type=str, help='The symbol or integer to encode/decode.')

# --- Varint Encoding/Decoding ---
varint_parser = subparsers.add_parser('varint', help='Varint operations.')
varint_parser.add_argument('operation', choices=['encode', 'decode'], help='The varint operation to perform.')
varint_parser.add_argument('value', type=str, help='The value to operate on.')

args = parser.parse_args()
obj = GlyphProtocol() 

if args.command == 'issue':
    obj.etch_glyph(
        args.name,
        args.divisibility,
        args.symbol,
        args.premine,
        args.mint_cap,
        args.mint_amount,
        args.start_height,
        args.end_height,
        args.start_offset,
        args.end_offset,
        args.destination_address,
        args.change_address,
        args.fee,
        args.live,
        args.nostr_pubkey,
    )
elif args.command == 'mint':
    obj.mint_glyph(
        args.glyph_id,
        args.amount,
        args.destination_address,
        args.change_address,
        args.fee,
        args.live,
        args.nostr_pubkey,
    )
elif args.command == 'transfer':
    obj.transfer_glyph(
        args.glyph_id,
        args.input_txid,
        args.input_vout,
        args.amount,
        args.destination_address,
        args.change_address,
        args.fee,
        args.live,
        args.nostr_pubkey,
    )
elif args.command == 'symbol':
    if args.action == 'encode':
        print(obj.symbol_to_int(args.value))
    elif args.action == 'decode':
        print(obj.int_to_symbol(int(args.value))) 
elif args.command == 'varint':
    if args.operation == 'encode':
        number = int(args.value)  
        encoded = obj.encode_varint(number)  
        print(encoded.hex())
    elif args.operation == 'decode':
        decoded = obj.decode_varint(bytes.fromhex(args.value))
        print(decoded)

if __name__ == '__main__':
main()
