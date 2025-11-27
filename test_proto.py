import base64

def extract_filename(b64_str):
    try:
        data = base64.b64decode(b64_str)
        # Protobuf manual parsing
        # Tag: (field_number << 3) | wire_type
        # 0x0A = (1 << 3) | 2  => Field 1, Length Delimited
        
        idx = 0
        while idx < len(data):
            tag = data[idx]
            idx += 1
            field_num = tag >> 3
            wire_type = tag & 0x7
            
            if wire_type != 2: # We only care about string/bytes
                # Skip logic would be complex without schema, but assuming field 1 is filename
                break
                
            # Read varint length
            length = 0
            shift = 0
            while True:
                if idx >= len(data): return None
                b = data[idx]
                idx += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            
            value = data[idx : idx + length]
            idx += length
            
            if field_num == 1:
                try:
                    return value.decode('utf-8')
                except:
                    pass
                    
    except Exception as e:
        print(e)
    return None

attr = 'Ch3ov5nmmK/kuIDkuKrmtYvor5Xmlofku7YuZG9jeCIEZmlsZUDnotu9hoDAAw=='
print(f"Extracted: {extract_filename(attr)}")
