# Rusty Glyphs
based on Melvin Carvalho glyphs and Runes protocols

Areas for Improvement:
Blockchain Querying: Implement the get_glyph_info and get_glyph_balance functions with actual blockchain interaction logic using the bitcoincore_rpc client.
Data Encoding and Decoding: The decode_glyphstone method is implemented, but a corresponding encode_glyphstone function might be useful for constructing Glyphstone data from structured data.
Testing: Implement comprehensive unit tests to ensure the correctness of the core Glyphs logic.

```mermaid
sequenceDiagram
    participant User
    participant CLI as Command Line Interface
    participant GP as GlyphProtocol
    participant BC as Bitcoin Core
    participant BN as Bitcoin Network

    Note over User,BN: Etch Glyph Operation
    User->>CLI: Execute 'issue' command with parameters
    CLI->>GP: Call etch_glyph()
    GP->>GP: Validate glyph name and parameters
    GP->>GP: Construct glyphstone data
    GP->>BC: Select UTXO for funding
    BC-->>GP: Return suitable UTXO
    GP->>GP: Create glyphstone output
    GP->>GP: Create premine output (if applicable)
    GP->>GP: Construct transaction
    alt Live mode
        GP->>BC: Sign transaction
        BC-->>GP: Return signed transaction
        GP->>BN: Broadcast transaction
        BN-->>GP: Return transaction ID
        GP-->>CLI: Return transaction ID
    else Dry run
        GP->>GP: Print transaction details
        GP-->>CLI: Return None
    end
    CLI-->>User: Display result

    Note over User,BN: Initiate Swap Operation
    User->>CLI: Execute 'initiate_swap' command
    CLI->>GP: Call initiate_swap()
    GP->>GP: Generate secret hash
    GP->>GP: Create HTLC script
    GP->>BC: Select UTXO for funding
    BC-->>GP: Return suitable UTXO
    GP->>GP: Construct HTLC output
    GP->>GP: Construct transaction
    GP->>BC: Sign transaction
    BC-->>GP: Return signed transaction
    GP->>BN: Broadcast transaction
    BN-->>GP: Return transaction ID
    GP-->>CLI: Return transaction ID and details for counterparty
    CLI-->>User: Display swap details and transaction ID

    Note over User,BN: Participate in Swap Operation
    User->>CLI: Execute 'participate_swap' command with received details
    CLI->>GP: Call participate_in_swap()
    GP->>GP: Validate received HTLC details
    GP->>GP: Create HTLC script based on received details
    GP->>BC: Select UTXO for funding
    BC-->>GP: Return suitable UTXO
    GP->>GP: Construct HTLC output
    GP->>GP: Construct transaction
    GP->>BC: Sign transaction
    BC-->>GP: Return signed transaction
    GP->>BN: Broadcast transaction
    BN-->>GP: Return transaction ID
    GP-->>CLI: Return transaction ID
    CLI-->>User: Display transaction ID

    Note over User,BN: Claim Glyph Operation
    User->>CLI: Execute 'claim_glyph' command
    CLI->>GP: Call claim_glyph()
    GP->>BC: Retrieve HTLC transaction
    BC-->>GP: Return HTLC transaction details
    GP->>GP: Construct claim script using secret
    GP->>GP: Construct claim transaction
    GP->>BC: Sign transaction
    BC-->>GP: Return signed transaction
    GP->>BN: Broadcast transaction
    BN-->>GP: Return transaction ID
    GP-->>CLI: Return transaction ID
    CLI-->>User: Display transaction ID

    Note over User,BN: Refund Glyph Operation
    User->>CLI: Execute 'refund_glyph' command
    CLI->>GP: Call refund_glyph()
    GP->>BC: Retrieve HTLC transaction
    BC-->>GP: Return HTLC transaction details
    GP->>GP: Construct refund script
    GP->>GP: Construct refund transaction
    GP->>BC: Sign transaction
    BC-->>GP: Return signed transaction
    GP->>BN: Broadcast transaction
    BN-->>GP: Return transaction ID
    GP-->>CLI: Return transaction ID
    CLI-->>User: Display transaction ID

```

