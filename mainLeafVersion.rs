use std::collections::HashMap;
use std::str::FromStr;
use bitcoin::{Address, Network, Script, Transaction, TxIn, TxOut, OutPoint, Txid};
use bitcoin::blockdata::opcodes::all::{OP_RETURN, OP_13, OP_DUP, OP_HASH160, OP_EQUALVERIFY, OP_CHECKSIG, OP_IF, OP_ELSE, OP_CHECKLOCKTIMEVERIFY, OP_DROP, OP_ENDIF};
use bitcoin::util::psbt::Input as PsbtInput;
use bitcoin::util::key::PublicKey;
use bitcoin::hashes::{Hash, sha256};
use secp256k1::Secp256k1;
use bitcoincore_rpc::{Auth, Client, RpcApi};
use clap::{App, Arg, SubCommand};
use thiserror::Error;
use unicode_categories::UnicodeCategories;

const COIN: u64 = 100_000_000;
const DEFAULT_SYMBOL_DIVISIBILITY: u8 = 8;
const DEFAULT_CURRENCY_SYMBOL: char = '¤';
const MAX_GLYPH_NAME_LENGTH: usize = 26;

#[derive(Error, Debug)]
enum GlyphError {
    #[error("Invalid symbol: {0}")]
    InvalidSymbol(String),
    #[error("Insufficient funds: {0}")]
    InsufficientFunds(String),
    #[error("Invalid transaction: {0}")]
    InvalidTransaction(String),
    #[error("Network error: {0}")]
    NetworkError(String),
    #[error("RPC error: {0}")]
    RpcError(#[from] bitcoincore_rpc::Error),
    #[error("Bitcoin error: {0}")]
    BitcoinError(#[from] bitcoin::util::Error),
}

struct GlyphProtocol {
    network: Network,
    rpc_client: Client,
    base_offset: u8,
}

impl GlyphProtocol {
    fn new(network: Network, rpc_url: &str, rpc_user: &str, rpc_pass: &str) -> Result<Self, GlyphError> {
        let rpc_client = Client::new(rpc_url, Auth::UserPass(rpc_user.to_string(), rpc_pass.to_string()))
            .map_err(GlyphError::RpcError)?;
        Ok(GlyphProtocol {
            network,
            rpc_client,
            base_offset: 1,
        })
    }

    fn symbol_to_int(&self, symbol: &str) -> Result<u64, GlyphError> {
        if !self.is_valid_glyph_name(symbol) {
            return Err(GlyphError::InvalidSymbol(format!("Invalid Glyph name: {}", symbol)));
        }

        let clean_symbol = symbol.replace('•', "");
        let mut value = 0u64;
        for (i, c) in clean_symbol.chars().rev().enumerate() {
            value += (c as u64 - 'A' as u64 + self.base_offset as u64) * 26u64.pow(i as u32);
        }
        Ok(value)
    }

    fn int_to_symbol(&self, num: u64) -> Result<String, GlyphError> {
        if self.base_offset == 1 && num == 0 {
            return Err(GlyphError::InvalidSymbol("Input must be a positive integer when using 1-26 numbering".to_string()));
        }

        let mut num = num;
        let mut symbol = String::new();
        let alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

        if self.base_offset == 0 && num == 0 {
            return Ok("A".to_string());
        }

        while num > 0 {
            num -= self.base_offset as u64;
            let remainder = (num % 26) as usize;
            symbol.push(alphabet.chars().nth(remainder).unwrap());
            num /= 26;
        }
        Ok(symbol.chars().rev().collect())
    }

    fn encode_varint(&self, i: u64) -> Vec<u8> {
        let mut encoded = Vec::new();
        let mut n = i;
        while n > 127 {
            encoded.push(((n & 0x7F) | 0x80) as u8);
            n >>= 7;
        }
        encoded.push(n as u8);
        encoded
    }

    fn decode_varint(&self, encoded: &[u8]) -> Result<(u64, usize), GlyphError> {
        let mut result = 0u64;
        let mut shift = 0;
        for (i, &byte) in encoded.iter().enumerate() {
            result |= ((byte & 0x7F) as u64) << shift;
            if byte & 0x80 == 0 {
                return Ok((result, i + 1));
            }
            shift += 7;
            if shift > 63 {
                return Err(GlyphError::InvalidTransaction("Invalid varint encoding".to_string()));
            }
        }
        Err(GlyphError::InvalidTransaction("Incomplete varint".to_string()))
    }

    fn select_utxo(&self, amount_needed_btc: f64) -> Result<PsbtInput, GlyphError> {
        let unspent = self.rpc_client.list_unspent(None, None, None, None, None)?;
        for utxo in unspent {
            if utxo.amount.to_btc() >= amount_needed_btc {
                return Ok(utxo);
            }
        }
        Err(GlyphError::InsufficientFunds(format!("No UTXO found with sufficient funds (needed: {} BTC)", amount_needed_btc)))
    }

    fn create_glyphstone_output(&self, glyphstone_data: &[u8]) -> TxOut {
        TxOut {
            value: 0,
            script_pubkey: Script::new_op_return(&[OP_13.into_u8(), glyphstone_data]),
        }
    }

    fn create_htlc_script(&self, receiver_pubkey: &PublicKey, sender_pubkey: &PublicKey, 
                          secret_hash: &[u8], timelock: u32) -> Script {
        Script::new()
            .push_opcode(OP_DUP)
            .push_opcode(OP_HASH160)
            .push_slice(secret_hash)
            .push_opcode(OP_EQUALVERIFY)
            .push_opcode(OP_CHECKSIG)
            .push_opcode(OP_IF)
            .push_key(receiver_pubkey)
            .push_opcode(OP_ELSE)
            .push_int(timelock as i64)
            .push_opcode(OP_CHECKLOCKTIMEVERIFY)
            .push_opcode(OP_DROP)
            .push_key(sender_pubkey)
            .push_opcode(OP_ENDIF)
            .push_opcode(OP_CHECKSIG)
    }

    fn etch_glyph(&self, name: &str, divisibility: u8, symbol: char, premine: u64,
                  mint_cap: Option<u64>, mint_amount: Option<u64>, 
                  start_height: Option<u32>, end_height: Option<u32>,
                  start_offset: Option<u32>, end_offset: Option<u32>,
                  destination_address: &str, change_address: Option<&str>,
                  fee_per_byte: u64, live: bool, nostr_pubkey: Option<&str>) -> Result<String, GlyphError> {
        let name_int = self.symbol_to_int(name)?;
        let mut glyphstone_data = vec![b'E'];
        glyphstone_data.extend_from_slice(&self.encode_varint(name_int));
        glyphstone_data.extend_from_slice(&self.encode_varint(divisibility as u64));

        glyphstone_data = self.add_optional_mint_params(glyphstone_data, symbol, premine, mint_cap, mint_amount,
                                                        start_height, end_height, start_offset, end_offset);

        let glyphstone_output = self.create_glyphstone_output(&glyphstone_data);
        
        let destination_output = if premine > 0 {
            Some(self.create_glyph_output(premine, divisibility, destination_address, nostr_pubkey)?)
        } else {
            None
        };

        self.construct_and_broadcast_transaction(glyphstone_output, destination_output, change_address, fee_per_byte, live)
    }

    fn mint_glyph(&self, glyph_id: &str, amount: u64, destination_address: &str,
                  change_address: Option<&str>, fee_per_byte: u64, live: bool,
                  nostr_pubkey: Option<&str>) -> Result<String, GlyphError> {
        let glyph_info = self.get_glyph_info(glyph_id)?;
        let current_height = self.rpc_client.get_block_count()? as u32;

        if !self.is_mint_open(&glyph_info, current_height) {
            return Err(GlyphError::InvalidTransaction(format!("Mint is closed for Glyph {}", glyph_id)));
        }

        if let Some(mint_amount) = glyph_info.get("mint_amount") {
            if amount != *mint_amount {
                return Err(GlyphError::InvalidTransaction(format!("Invalid mint amount. Must be {}", mint_amount)));
            }
        }

        let (block_height, tx_index) = Self::parse_glyph_id(glyph_id)?;
        let mut glyphstone_data = vec![b'M'];
        glyphstone_data.extend_from_slice(&self.encode_varint(block_height as u64));
        glyphstone_data.extend_from_slice(&self.encode_varint(tx_index as u64));
        glyphstone_data.extend_from_slice(&self.encode_varint(amount));

        let glyphstone_output = self.create_glyphstone_output(&glyphstone_data);
        
        let destination_output = self.create_glyph_output(amount, *glyph_info.get("divisibility").unwrap() as u8, destination_address, nostr_pubkey)?;

        self.construct_and_broadcast_transaction(glyphstone_output, Some(destination_output), change_address, fee_per_byte, live)
    }

    fn transfer_glyph(&self, glyph_id: &str, input_txid: &str, input_vout: u32, amount: u64,
                      destination_address: &str, change_address: Option<&str>,
                      fee_per_byte: u64, live: bool, nostr_pubkey: Option<&str>) -> Result<String, GlyphError> {
        let (block_height, tx_index) = Self::parse_glyph_id(glyph_id)?;
        
        let input_glyphs = self.get_glyph_balance(input_txid, input_vout, glyph_id)?;
        if input_glyphs < amount {
            return Err(GlyphError::InsufficientFunds(format!("Insufficient Glyphs in input. Available: {}, Requested: {}", input_glyphs, amount)));
        }

        let mut glyphstone_data = vec![b'T'];
        glyphstone_data.extend_from_slice(&self.encode_varint(block_height as u64));
        glyphstone_data.extend_from_slice(&self.encode_varint(tx_index as u64));
        glyphstone_data.extend_from_slice(&self.encode_varint(amount));
        glyphstone_data.extend_from_slice(&self.encode_varint(1)); // Output index 1 for the destination output

        let glyphstone_output = self.create_glyphstone_output(&glyphstone_data);

        let txin = TxIn {
            previous_output: OutPoint::new(Txid::from_str(input_txid).map_err(|_| GlyphError::InvalidTransaction("Invalid input txid".to_string()))?, input_vout),
            script_sig: Script::new(),
            sequence: 0xFFFFFFFF,
            witness: vec![],
        };

        let destination_output = if destination_address.starts_with("OP_RETURN") {
            TxOut { value: 0, script_pubkey: Script::new_op_return(&[]) }
        } else {
            self.create_glyph_output(amount, 0, destination_address, nostr_pubkey)?
        };

        self.construct_and_broadcast_transaction(glyphstone_output, Some(destination_output), change_address, fee_per_byte, live)
    }

    fn is_valid_glyph_name(&self, name: &str) -> bool {
        if name.is_empty() || name.len() > MAX_GLYPH_NAME_LENGTH {
            return false;
        }

        let mut letter_count = 0;
        for (i, c) in name.chars().enumerate() {
            if c.is_ascii_uppercase() {
                letter_count += 1;
            } else if c == '•' {
                if i == 0 || i == name.len() - 1 || name.chars().nth(i-1) == Some('•') || name.chars().nth(i+1) == Some('•') {
                    return false;
                }
            } else {
                return false;
            }
        }

        letter_count > 0 && letter_count <= MAX_GLYPH_NAME_LENGTH
    }

    fn is_valid_currency_symbol(&self, symbol: char) -> bool {
        let category = symbol.general_category();
        !(category.is_letter() || category.is_number())
    }

    fn is_mint_open(&self, glyph_info: &HashMap<String, u64>, current_height: u32) -> bool {
        if let (Some(mint_cap), Some(minted_count)) = (glyph_info.get("mint_cap"), glyph_info.get("minted_count")) {
            if minted_count >= mint_cap {
                return false;
            }
        }

        let etch_height = *glyph_info.get("etch_height").unwrap_or(&0);
        let start_height = glyph_info.get("start_height")
            .map(|&h| h)
            .or_else(|| glyph_info.get("start_offset").map(|&o| etch_height + o))
            .unwrap_or(0);
        let end_height = glyph_info.get("end_height")
            .map(|&h| h)
            .or_else(|| glyph_info.get("end_offset").map(|&o| etch_height + o))
            .unwrap_or(u32::MAX);

        (start_height..end_height).contains(&current_height)
    }

    fn get_glyph_info(&self, glyph_id: &str) -> Result<HashMap<String, u64>, GlyphError> {
        let (block_height, tx_index) = Self::parse_glyph_id(glyph_id)?;
        let block_hash = self.rpc_client.get_block_hash(block_height as u64)?;
        let block = self.rpc_client.get_block(&block_hash)?;
    
        if tx_index >= block.txdata.len() as u32 {
            return Err(GlyphError::InvalidTransaction(format!("Transaction index {} out of range for block {}", tx_index, block_height)));
        }
    
        let transaction = &block.txdata[tx_index as usize];
    
        let glyphstone_data = transaction.output.iter()
            .filter_map(|output| {
                if output.script_pubkey.is_op_return() {
                    let script_asm = output.script_pubkey.asm();
                    let parts: Vec<&str> = script_asm.split_whitespace().collect();
                    if parts.len() > 2 && parts[1] == "OP_13" {
                        Some(hex::decode(parts[2]).unwrap())
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .next()
            .ok_or_else(|| GlyphError::InvalidTransaction(format!("Glyph not found with ID: {}", glyph_id)))?;
    
        self.decode_glyphstone(&glyphstone_data, block_height)
    }
    
    fn decode_glyphstone(&self, glyphstone_data: &[u8], etch_height: u32) -> Result<HashMap<String, u64>, GlyphError> {
        let mut glyph_info = HashMap::new();
        glyph_info.insert("etch_height".to_string(), etch_height as u64);
        glyph_info.insert("minted_count".to_string(), 0);
    
        if glyphstone_data[0] != b'E' {
            return Err(GlyphError::InvalidTransaction("Invalid Glyphstone data: doesn't start with 'E'".to_string()));
        }
    
        let mut data = &glyphstone_data[1..];
        let (name_int, rest) = self.decode_varint(data)?;
        glyph_info.insert("name".to_string(), name_int);
        
        let (divisibility, rest) = self.decode_varint(rest)?;
        glyph_info.insert("divisibility".to_string(), divisibility);
    
        data = rest;
    
        if !data.is_empty() {
            glyph_info.insert("symbol".to_string(), data[0] as u64);
            data = &data[1..];
        }
    
        while !data.is_empty() {
            match data[0] {
                b'C' => {
                    let (mint_cap, rest) = self.decode_varint(&data[1..])?;
                    glyph_info.insert("mint_cap".to_string(), mint_cap);
                    data = rest;
                },
                b'A' => {
                    let (mint_amount, rest) = self.decode_varint(&data[1..])?;
                    glyph_info.insert("mint_amount".to_string(), mint_amount);
                    data = rest;
                },
                b'S' => {
                    let (start_height, rest) = self.decode_varint(&data[1..])?;
                    glyph_info.insert("start_height".to_string(), start_height);
                    data = rest;
                },
                b'H' => {
                    let (end_height, rest) = self.decode_varint(&data[1..])?;
                    glyph_info.insert("end_height".to_string(), end_height);
                    data = rest;
                },
                b'O' => {
                    let (start_offset, rest) = self.decode_varint(&data[1..])?;
                    glyph_info.insert("start_offset".to_string(), start_offset);
                    data = rest;
                },
                b'F' => {
                    let (end_offset, rest) = self.decode_varint(&data[1..])?;
                    glyph_info.insert("end_offset".to_string(), end_offset);
                    data = rest;
                },
                _ => break,
            }
        }
    
        Ok(glyph_info)
    }
    
    fn get_glyph_balance(&self, txid: &str, vout: u32, glyph_id: &str) -> Result<u64, GlyphError> {
        let raw_tx = self.rpc_client.get_raw_transaction_verbose(&Txid::from_str(txid).map_err(|_| GlyphError::InvalidTransaction("Invalid txid".to_string()))?)?;
        
        if vout as usize >= raw_tx.vout.len() {
            return Err(GlyphError::InvalidTransaction(format!("Output index {} out of range for transaction {}", vout, txid)));
        }
    
        let output = &raw_tx.vout[vout as usize];
    
        // Check if the output is unspent
        if self.rpc_client.get_tx_out(&Txid::from_str(txid).unwrap(), vout, Some(true)).is_none() {
            return Err(GlyphError::InvalidTransaction(format!("UTXO {}:{} has been spent", txid, vout)));
        }
    
        let script_asm = &output.script_pub_key.asm;
        let parts: Vec<&str> = script_asm.split_whitespace().collect();
        
        if parts.len() < 3 || parts[1] != "OP_13" {
            return Err(GlyphError::InvalidTransaction(format!("UTXO {}:{} does not contain valid Glyphstone data", txid, vout)));
        }
    
        let glyphstone_data = hex::decode(parts[2]).map_err(|_| GlyphError::InvalidTransaction("Invalid Glyphstone data".to_string()))?;
        self.decode_glyph_balance(&glyphstone_data, glyph_id)
    }
    
    fn decode_glyph_balance(&self, glyphstone_data: &[u8], glyph_id: &str) -> Result<u64, GlyphError> {
        if glyphstone_data[0] != b'T' {
            return Err(GlyphError::InvalidTransaction("Invalid Glyphstone data: doesn't start with 'T'".to_string()));
        }
    
        let mut data = &glyphstone_data[1..];
        let (block_height, rest) = self.decode_varint(data)?;
        let (tx_index, rest) = self.decode_varint(rest)?;
    
        if format!("{}:{}", block_height, tx_index) != glyph_id {
            return Err(GlyphError::InvalidTransaction(format!("Glyphstone does not contain Glyph with ID {}", glyph_id)));
        }
    
        let (balance, _) = self.decode_varint(rest)?;
        Ok(balance)
    }
    
    fn construct_and_broadcast_transaction(&self, glyphstone_output: TxOut,
                                           destination_output: Option<TxOut>,
                                           change_address: Option<&str>,
                                           fee_per_byte: u64, live: bool) -> Result<String, GlyphError> {
        let amount_needed_btc = 0.0001; // Initial estimate
        let utxo = self.select_utxo(amount_needed_btc)?;
    
        let mut tx = Transaction {
            version: 2,
            lock_time: 0,
            input: vec![TxIn {
                previous_output: OutPoint::new(utxo.txid, utxo.vout),
                script_sig: Script::new(),
                sequence: 0xFFFFFFFF,
                witness: vec![],
            }],
            output: vec![glyphstone_output],
        };
    
        if let Some(dest_output) = destination_output {
            tx.output.push(dest_output);
        }
    
        let tx_size = tx.get_weight() as u64;
        let fee = tx_size * fee_per_byte;
    
        if let Some(change_addr) = change_address {
            let change = utxo.amount.to_sat() - fee - tx.output.iter().map(|o| o.value).sum::<u64>();
            if change > 0 {
                let change_script = Address::from_str(change_addr)?.script_pubkey();
                tx.output.push(TxOut {
                    value: change,
                    script_pubkey: change_script,
                });
            }
        }
    
        if self.is_cenotaph(&glyphstone_output) {
            println!("Warning: Malformed glyphstone detected. Treating as cenotaph.");
            tx.output = vec![TxOut {
                value: 0,
                script_pubkey: Script::new_op_return(&[]),
            }];
        }
    
        if live {
            let signed_tx = self.rpc_client.sign_raw_transaction_with_wallet(&tx, None, None)?;
            let txid = self.rpc_client.send_raw_transaction(&signed_tx.hex)?;
            Ok(txid.to_string())
        } else {
            println!("{:#?}", tx);
            Ok(tx.txid().to_string())
        }
    }
    
    fn is_cenotaph(&self, glyphstone_output: &TxOut) -> bool {
        let script = &glyphstone_output.script_pubkey;
        script.len() < 2 || script[0] != OP_RETURN.into_u8() || script[1] != OP_13.into_u8()
    }
    
    fn create_taproot_address(&self, bitcoin_address: &str, nostr_pubkey: Option<&str>) -> Result<Address, GlyphError> {
        let addr = Address::from_str(bitcoin_address)?;
        let script_pubkey = addr.script_pubkey();
        
        let nostr_leaf = if let Some(pubkey) = nostr_pubkey {
            Script::new_v1_p2tr(&Secp256k1::new(), &PublicKey::from_str(pubkey)?, None)
        } else {
            Script::new()
        };
    
        let taproot_script = Script::new_v1_p2tr(&Secp256k1::new(), &PublicKey::from_slice(&script_pubkey[1..])?, Some(nostr_leaf));
        
        Ok(Address::p2tr(&Secp256k1::new(), taproot_script.to_inner()[1..33].try_into().unwrap(), None, self.network))
    }
    
    fn add_optional_mint_params(&self, mut glyphstone_data: Vec<u8>, symbol: char, premine: u64,
                                mint_cap: Option<u64>, mint_amount: Option<u64>, 
                                start_height: Option<u32>, end_height: Option<u32>,
                                start_offset: Option<u32>, end_offset: Option<u32>) -> Vec<u8> {
        glyphstone_data.push(symbol as u8);
        glyphstone_data.extend_from_slice(&self.encode_varint(premine));
        
        if let Some(cap) = mint_cap {
            glyphstone_data.push(b'C');
            glyphstone_data.extend_from_slice(&self.encode_varint(cap));
        }
        if let Some(amount) = mint_amount {
            glyphstone_data.push(b'A');
            glyphstone_data.extend_from_slice(&self.encode_varint(amount));
        }
        if let Some(height) = start_height {
            glyphstone_data.push(b'S');
            glyphstone_data.extend_from_slice(&self.encode_varint(height as u64));
        }
        if let Some(height) = end_height {
            glyphstone_data.push(b'H');
            glyphstone_data.extend_from_slice(&self.encode_varint(height as u64));
        }
        if let Some(offset) = start_offset {
            glyphstone_data.push(b'O');
            glyphstone_data.extend_from_slice(&self.encode_varint(offset as u64));
        }
        if let Some(offset) = end_offset {
            glyphstone_data.push(b'F');
            glyphstone_data.extend_from_slice(&self.encode_varint(offset as u64));
        }
        
        glyphstone_data
    }
    
    fn create_glyph_output(&self, amount: u64, divisibility: u8,
                           destination_address: &str, nostr_pubkey: Option<&str>) -> Result<TxOut, GlyphError> {
        if amount > 0 {
            if destination_address.is_empty() {
                return Err(GlyphError::InvalidTransaction("Destination address is required for a non-zero amount of Glyphs".to_string()));
            }
            let destination_address_obj = if let Some(pubkey) = nostr_pubkey {
                self.create_taproot_address(destination_address, Some(pubkey))?
            } else {
                Address::from_str(destination_address)?
            };
            let output_value = amount * 10u64.pow(divisibility as u32);
            Ok(TxOut {
                value: output_value,
                script_pubkey: destination_address_obj.script_pubkey(),
            })
        } else {
            Ok(TxOut {
                value: 0,
                script_pubkey: Script::new(),
            })
        }
    }
    
    fn parse_glyph_id(glyph_id: &str) -> Result<(u32, u32), GlyphError> {
        let parts: Vec<&str> = glyph_id.split(':').collect();
        if parts.len() != 2 {
            return Err(GlyphError::InvalidTransaction(format!("Invalid glyph_id format: {}", glyph_id)));
        }
        let block_height = parts[0].parse().map_err(|_| GlyphError::InvalidTransaction(format!("Invalid block height in glyph_id: {}", glyph_id)))?;
        let tx_index = parts[1].parse().map_err(|_| GlyphError::InvalidTransaction(format!("Invalid transaction index in glyph_id: {}", glyph_id)))?;
        Ok((block_height, tx_index))
    }

    fn initiate_swap(&self, glyph_id: &str, amount: u64, destination_address: &str,
        counterparty_pubkey: &str, secret: &str, timelock: u32) -> Result<String, GlyphError> {
let secret_hash = sha256::Hash::hash(secret.as_bytes());
let receiver_pubkey = PublicKey::from_str(counterparty_pubkey)
.map_err(|e| GlyphError::InvalidTransaction(format!("Invalid counterparty pubkey: {}", e)))?;
let sender_pubkey = self.get_pubkey_from_address(destination_address)?;

let htlc_script = self.create_htlc_script(&receiver_pubkey, &sender_pubkey, secret_hash.as_inner(), timelock);

let htlc_output = TxOut {
value: amount,
script_pubkey: htlc_script,
};

self.construct_and_broadcast_transaction(htlc_output, None, Some(self.rpc_client.get_new_address(None, None)?.to_string().as_str()), 1, true)
}

fn participate_in_swap(&self, glyph_id: &str, amount: u64, 
              counterparty_htlc_details: &HashMap<String, String>,
              destination_address: &str) -> Result<String, GlyphError> {
let secret_hash = hex::decode(&counterparty_htlc_details["secret_hash"])
.map_err(|e| GlyphError::InvalidTransaction(format!("Invalid secret hash: {}", e)))?;
let receiver_pubkey = PublicKey::from_str(&counterparty_htlc_details["receiver_pubkey"])
.map_err(|e| GlyphError::InvalidTransaction(format!("Invalid receiver pubkey: {}", e)))?;
let sender_pubkey = self.get_pubkey_from_address(destination_address)?;
let timelock: u32 = counterparty_htlc_details["timelock"].parse()
.map_err(|e| GlyphError::InvalidTransaction(format!("Invalid timelock: {}", e)))?;

let htlc_script = self.create_htlc_script(&receiver_pubkey, &sender_pubkey, &secret_hash, timelock);

let htlc_output = TxOut {
value: amount,
script_pubkey: htlc_script,
};

self.construct_and_broadcast_transaction(htlc_output, None, Some(self.rpc_client.get_new_address(None, None)?.to_string().as_str()), 1, true)
}

fn claim_glyph(&self, htlc_txid: &str, secret: &str, destination_address: &str) -> Result<String, GlyphError> {
let htlc_tx = self.rpc_client.get_transaction(
&Txid::from_str(htlc_txid).map_err(|_| GlyphError::InvalidTransaction("Invalid HTLC txid".to_string()))?
)?;

let htlc_output = htlc_tx.vout.iter()
.find(|output| output.script_pub_key.asm.contains("OP_HASH160"))
.ok_or_else(|| GlyphError::InvalidTransaction("HTLC output not found".to_string()))?;

let secret_bytes = secret.as_bytes();
let claim_script = Script::new()
.push_slice(secret_bytes)
.push_opcode(OP_TRUE);

let tx_in = TxIn {
previous_output: OutPoint::new(Txid::from_str(htlc_txid)?, htlc_output.n),
script_sig: claim_script,
sequence: 0xFFFFFFFF,
witness: vec![],
};

let destination_address_obj = Address::from_str(destination_address)?;
let tx_out = TxOut {
value: htlc_output.value.to_sat(),
script_pubkey: destination_address_obj.script_pubkey(),
};

let tx = Transaction {
version: 2,
lock_time: 0,
input: vec![tx_in],
output: vec![tx_out],
};

let signed_tx = self.rpc_client.sign_raw_transaction_with_wallet(&tx, None, None)?;
let txid = self.rpc_client.send_raw_transaction(&signed_tx.hex)?;
Ok(txid.to_string())
}

fn refund_glyph(&self, htlc_txid: &str, destination_address: &str) -> Result<String, GlyphError> {
let htlc_tx = self.rpc_client.get_transaction(
&Txid::from_str(htlc_txid).map_err(|_| GlyphError::InvalidTransaction("Invalid HTLC txid".to_string()))?
)?;

let htlc_output = htlc_tx.vout.iter()
.find(|output| output.script_pub_key.asm.contains("OP_HASH160"))
.ok_or_else(|| GlyphError::InvalidTransaction("HTLC output not found".to_string()))?;

let refund_script = Script::new().push_opcode(OP_FALSE);

let tx_in = TxIn {
previous_output: OutPoint::new(Txid::from_str(htlc_txid)?, htlc_output.n),
script_sig: refund_script,
sequence: 0xFFFFFFFF,
witness: vec![],
};

let destination_address_obj = Address::from_str(destination_address)?;
let tx_out = TxOut {
value: htlc_output.value.to_sat(),
script_pubkey: destination_address_obj.script_pubkey(),
};

let tx = Transaction {
version: 2,
lock_time: 0,
input: vec![tx_in],
output: vec![tx_out],
};

let signed_tx = self.rpc_client.sign_raw_transaction_with_wallet(&tx, None, None)?;
let txid = self.rpc_client.send_raw_transaction(&signed_tx.hex)?;
Ok(txid.to_string())
}

fn get_pubkey_from_address(&self, address: &str) -> Result<PublicKey, GlyphError> {
let address_info = self.rpc_client.get_address_info(address)?;
PublicKey::from_str(&address_info.pubkey.ok_or_else(|| GlyphError::InvalidTransaction("No pubkey found for address".to_string()))?)
.map_err(|e| GlyphError::InvalidTransaction(format!("Invalid pubkey for address: {}", e)))
}
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
let matches = App::new("Glyph Protocol CLI")
.version("1.0")
.author("Your Name")
.about("Interacts with the Glyph Protocol on Bitcoin")
.subcommand(SubCommand::with_name("symbol")
.about("Encode or decode a Glyph symbol")
.arg(Arg::with_name("action")
   .required(true)
   .possible_values(&["encode", "decode"])
   .help("Whether to encode or decode the symbol"))
.arg(Arg::with_name("value")
   .required(true)
   .help("The symbol or integer to encode/decode")))
.subcommand(SubCommand::with_name("issue")
.about("Issue a new Glyph")
.arg(Arg::with_name("name")
   .required(true)
   .help("Name of the Glyph to be issued"))
.arg(Arg::with_name("divisibility")
   .long("divisibility")
   .takes_value(true)
   .default_value("8")
   .help("Number of decimal places for the Glyph"))
.arg(Arg::with_name("symbol")
   .long("symbol")
   .takes_value(true)
   .default_value("¤")
   .help("Currency symbol for the Glyph"))
.arg(Arg::with_name("premine")
   .long("premine")
   .takes_value(true)
   .default_value("0")
   .help("Amount of Glyphs to premine"))
.arg(Arg::with_name("mint_cap")
   .long("mint_cap")
   .takes_value(true)
   .help("Optional cap on the number of mints allowed"))
.arg(Arg::with_name("mint_amount")
   .long("mint_amount")
   .takes_value(true)
   .help("Optional fixed amount of Glyphs to be minted per transaction"))
.arg(Arg::with_name("start_height")
   .long("start_height")
   .takes_value(true)
   .help("Optional block height to start the open mint"))
.arg(Arg::with_name("end_height")
   .long("end_height")
   .takes_value(true)
   .help("Optional block height to end the open mint"))
.arg(Arg::with_name("start_offset")
   .long("start_offset")
   .takes_value(true)
   .help("Optional block offset from the etch block to start the open mint"))
.arg(Arg::with_name("end_offset")
   .long("end_offset")
   .takes_value(true)
   .help("Optional block offset from the etch block to end the open mint"))
.arg(Arg::with_name("destination_address")
   .long("destination_address")
   .takes_value(true)
   .help("Destination address for premined Glyphs"))
.arg(Arg::with_name("change_address")
   .long("change_address")
   .takes_value(true)
   .help("Change address for Bitcoin"))
.arg(Arg::with_name("fee")
   .long("fee")
   .takes_value(true)
   .default_value("1")
   .help("Transaction fee in satoshis per byte"))
.arg(Arg::with_name("live")
   .long("live")
   .help("Broadcast the transaction to the network"))
.arg(Arg::with_name("nostr_pubkey")
   .long("nostr_pubkey")
   .takes_value(true)
   .help("Optional Nostr public key to integrate via Taproot")))
.subcommand(SubCommand::with_name("mint")
.about("Mint new units of a Glyph")
.arg(Arg::with_name("glyph_id")
   .required(true)
   .help("Glyph ID to mint in BLOCK:TX format"))
.arg(Arg::with_name("amount")
   .required(true)
   .help("Amount of Glyphs to mint"))
.arg(Arg::with_name("destination_address")
   .required(true)
   .help("Destination address for the minted Glyphs"))
.arg(Arg::with_name("change_address")
   .long("change_address")
   .takes_value(true)
   .help("Change address for Bitcoin"))
.arg(Arg::with_name("fee")
   .long("fee")
   .takes_value(true)
   .default_value("1")
   .help("Transaction fee in satoshis per byte"))
.arg(Arg::with_name("live")
   .long("live")
   .help("Broadcast the transaction to the network"))
.arg(Arg::with_name("nostr_pubkey")
   .long("nostr_pubkey")
   .takes_value(true)
   .help("Optional Nostr public key to integrate via Taproot")))
.subcommand(SubCommand::with_name("transfer")
.about("Transfer Glyphs")
.arg(Arg::with_name("glyph_id")
   .required(true)
   .help("Glyph ID to transfer in BLOCK:TX format"))
.arg(Arg::with_name("input_txid")
   .required(true)
   .help("Transaction ID of the input UTXO"))
.arg(Arg::with_name("input_vout")
   .required(true)
   .help("Output index of the input UTXO"))
.arg(Arg::with_name("amount")
   .required(true)
   .help("Amount of Glyphs to transfer"))
.arg(Arg::with_name("destination_address")
   .required(true)
   .help("Destination address for the Glyphs"))
.arg(Arg::with_name("change_address")
   .long("change_address")
   .takes_value(true)
   .help("Change address for Bitcoin and remaining Glyphs"))
.arg(Arg::with_name("fee")
   .long("fee")
   .takes_value(true)
   .default_value("1")
   .help("Transaction fee in satoshis per byte"))
.arg(Arg::with_name("live")
   .long("live")
   .help("Broadcast the transaction to the network"))
.arg(Arg::with_name("nostr_pubkey")
   .long("nostr_pubkey")
   .takes_value(true)
   .help("Optional Nostr public key to integrate via Taproot")))
.subcommand(SubCommand::with_name("initiate_swap")
.about("Initiate an atomic swap")
.arg(Arg::with_name("glyph_id")
   .required(true)
   .help("Glyph ID to swap in BLOCK:TX format"))
.arg(Arg::with_name("amount")
   .required(true)
   .help("Amount of Glyphs to swap"))
.arg(Arg::with_name("destination_address")
   .required(true)
   .help("Your Bitcoin address"))
.arg(Arg::with_name("counterparty_pubkey")
   .required(true)
   .help("Counterparty's public key"))
.arg(Arg::with_name("secret")
   .required(true)
   .help("Secret for the HTLC"))
.arg(Arg::with_name("timelock")
   .required(true)
   .help("Timelock for the HTLC")))
.subcommand(SubCommand::with_name("participate_swap")
.about("Participate in an atomic swap")
.arg(Arg::with_name("glyph_id")
   .required(true)
   .help("Glyph ID to swap in BLOCK:TX format"))
.arg(Arg::with_name("amount")
   .required(true)
   .help("Amount of Glyphs to swap"))
.arg(Arg::with_name("destination_address")
   .required(true)
   .help("Your Bitcoin address"))
.arg(Arg::with_name("secret_hash")
   .required(true)
   .help("Hash of the secret provided by the counterparty"))
.arg(Arg::with_name("counterparty_pubkey")
   .required(true)
   .help("Counterparty's public key"))
.arg(Arg::with_name("timelock")
   .required(true)
   .help("Timelock for the HTLC")))
   .subcommand(SubCommand::with_name("claim_glyph")
   .about("Claim Glyphs from an HTLC")
   .arg(Arg::with_name("htlc_txid")
       .required(true)
       .help("Transaction ID of the HTLC"))
   .arg(Arg::with_name("secret")
       .required(true)
       .help("Secret to claim the HTLC"))
   .arg(Arg::with_name("destination_address")
       .required(true)
       .help("Destination address for the claimed Glyphs")))
.subcommand(SubCommand::with_name("refund_glyph")
   .about("Refund Glyphs from an expired HTLC")
   .arg(Arg::with_name("htlc_txid")
       .required(true)
       .help("Transaction ID of the HTLC"))
   .arg(Arg::with_name("destination_address")
       .required(true)
       .help("Destination address for the refunded Glyphs")))
.get_matches();

let glyph_protocol = GlyphProtocol::new(Network::Testnet, "http://localhost:18332", "rpcuser", "rpcpassword")?;

match matches.subcommand() {
("symbol", Some(symbol_matches)) => {
   let action = symbol_matches.value_of("action").unwrap();
   let value = symbol_matches.value_of("value").unwrap();
   match action {
       "encode" => {
           match glyph_protocol.symbol_to_int(value) {
               Ok(encoded) => println!("Encoded value: {}", encoded),
               Err(e) => eprintln!("Error: {}", e),
           }
       },
       "decode" => {
           let num: u64 = value.parse().map_err(|_| GlyphError::InvalidSymbol("Invalid integer".to_string()))?;
           match glyph_protocol.int_to_symbol(num) {
               Ok(symbol) => println!("Decoded symbol: {}", symbol),
               Err(e) => eprintln!("Error: {}", e),
           }
       },
       _ => unreachable!(),
   }
},
("issue", Some(issue_matches)) => {
   let name = issue_matches.value_of("name").unwrap();
   let divisibility = issue_matches.value_of("divisibility").unwrap().parse()?;
   let symbol = issue_matches.value_of("symbol").unwrap().chars().next().unwrap();
   let premine = issue_matches.value_of("premine").unwrap().parse()?;
   let mint_cap = issue_matches.value_of("mint_cap").map(|s| s.parse().unwrap());
   let mint_amount = issue_matches.value_of("mint_amount").map(|s| s.parse().unwrap());
   let start_height = issue_matches.value_of("start_height").map(|s| s.parse().unwrap());
   let end_height = issue_matches.value_of("end_height").map(|s| s.parse().unwrap());
   let start_offset = issue_matches.value_of("start_offset").map(|s| s.parse().unwrap());
   let end_offset = issue_matches.value_of("end_offset").map(|s| s.parse().unwrap());
   let destination_address = issue_matches.value_of("destination_address").unwrap_or("");
   let change_address = issue_matches.value_of("change_address");
   let fee_per_byte = issue_matches.value_of("fee").unwrap().parse()?;
   let live = issue_matches.is_present("live");
   let nostr_pubkey = issue_matches.value_of("nostr_pubkey");

   match glyph_protocol.etch_glyph(name, divisibility, symbol, premine, mint_cap, mint_amount,
                                   start_height, end_height, start_offset, end_offset,
                                   destination_address, change_address, fee_per_byte, live, nostr_pubkey) {
       Ok(txid) => println!("Glyph issued successfully. Transaction ID: {}", txid),
       Err(e) => eprintln!("Error: {}", e),
   }
},
("mint", Some(mint_matches)) => {
   let glyph_id = mint_matches.value_of("glyph_id").unwrap();
   let amount = mint_matches.value_of("amount").unwrap().parse()?;
   let destination_address = mint_matches.value_of("destination_address").unwrap();
   let change_address = mint_matches.value_of("change_address");
   let fee_per_byte = mint_matches.value_of("fee").unwrap().parse()?;
   let live = mint_matches.is_present("live");
   let nostr_pubkey = mint_matches.value_of("nostr_pubkey");

   match glyph_protocol.mint_glyph(glyph_id, amount, destination_address, change_address, fee_per_byte, live, nostr_pubkey) {
       Ok(txid) => println!("Glyphs minted successfully. Transaction ID: {}", txid),
       Err(e) => eprintln!("Error: {}", e),
   }
},
("transfer", Some(transfer_matches)) => {
   let glyph_id = transfer_matches.value_of("glyph_id").unwrap();
   let input_txid = transfer_matches.value_of("input_txid").unwrap();
   let input_vout = transfer_matches.value_of("input_vout").unwrap().parse()?;
   let amount = transfer_matches.value_of("amount").unwrap().parse()?;
   let destination_address = transfer_matches.value_of("destination_address").unwrap();
   let change_address = transfer_matches.value_of("change_address");
   let fee_per_byte = transfer_matches.value_of("fee").unwrap().parse()?;
   let live = transfer_matches.is_present("live");
   let nostr_pubkey = transfer_matches.value_of("nostr_pubkey");

   match glyph_protocol.transfer_glyph(glyph_id, input_txid, input_vout, amount, destination_address, change_address, fee_per_byte, live, nostr_pubkey) {
       Ok(txid) => println!("Glyphs transferred successfully. Transaction ID: {}", txid),
       Err(e) => eprintln!("Error: {}", e),
   }
},
("initiate_swap", Some(initiate_matches)) => {
   let glyph_id = initiate_matches.value_of("glyph_id").unwrap();
   let amount = initiate_matches.value_of("amount").unwrap().parse()?;
   let destination_address = initiate_matches.value_of("destination_address").unwrap();
   let counterparty_pubkey = initiate_matches.value_of("counterparty_pubkey").unwrap();
   let secret = initiate_matches.value_of("secret").unwrap();
   let timelock = initiate_matches.value_of("timelock").unwrap().parse()?;

   match glyph_protocol.initiate_swap(glyph_id, amount, destination_address, counterparty_pubkey, secret, timelock) {
       Ok(txid) => {
           println!("Swap initiated successfully. Transaction ID: {}", txid);
           println!("Provide the following details to your counterparty:");
           println!("Glyph ID: {}", glyph_id);
           println!("Amount: {}", amount);
           println!("Secret Hash: {}", hex::encode(sha256::Hash::hash(secret.as_bytes())));
           println!("Timelock: {}", timelock);
           println!("Your Public Key: {}", glyph_protocol.get_pubkey_from_address(destination_address)?);
       },
       Err(e) => eprintln!("Error: {}", e),
   }
},
("participate_swap", Some(participate_matches)) => {
   let glyph_id = participate_matches.value_of("glyph_id").unwrap();
   let amount = participate_matches.value_of("amount").unwrap().parse()?;
   let destination_address = participate_matches.value_of("destination_address").unwrap();
   let secret_hash = participate_matches.value_of("secret_hash").unwrap();
   let counterparty_pubkey = participate_matches.value_of("counterparty_pubkey").unwrap();
   let timelock = participate_matches.value_of("timelock").unwrap().parse()?;

   let mut counterparty_htlc_details = HashMap::new();
   counterparty_htlc_details.insert("secret_hash".to_string(), secret_hash.to_string());
   counterparty_htlc_details.insert("receiver_pubkey".to_string(), counterparty_pubkey.to_string());
   counterparty_htlc_details.insert("timelock".to_string(), timelock.to_string());

   match glyph_protocol.participate_in_swap(glyph_id, amount, &counterparty_htlc_details, destination_address) {
       Ok(txid) => println!("Successfully participated in swap. Transaction ID: {}", txid),
       Err(e) => eprintln!("Error: {}", e),
   }
},
("claim_glyph", Some(claim_matches)) => {
   let htlc_txid = claim_matches.value_of("htlc_txid").unwrap();
   let secret = claim_matches.value_of("secret").unwrap();
   let destination_address = claim_matches.value_of("destination_address").unwrap();

   match glyph_protocol.claim_glyph(htlc_txid, secret, destination_address) {
       Ok(txid) => println!("Glyphs claimed successfully. Transaction ID: {}", txid),
       Err(e) => eprintln!("Error: {}", e),
   }
},
("refund_glyph", Some(refund_matches)) => {
   let htlc_txid = refund_matches.value_of("htlc_txid").unwrap();
   let destination_address = refund_matches.value_of("destination_address").unwrap();

   match glyph_protocol.refund_glyph(htlc_txid, destination_address) {
       Ok(txid) => println!("Glyphs refunded successfully. Transaction ID: {}", txid),
       Err(e) => eprintln!("Error: {}", e),
   }
},
_ => println!("Invalid command. Use --help for usage information."),
}

Ok(())
}
