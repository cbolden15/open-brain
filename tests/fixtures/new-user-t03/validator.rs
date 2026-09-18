// This module is compiled in the actual desktop crate and uses its transport parser.
use super::from_slice;
use serde_json::Value;
fn valid(rule: &Value, value: &Value, contract: &Value) -> bool {
    if let Some(name) = rule["ref"].as_str() {
        return valid(&contract["definitions"][name], value, contract);
    }
    if let Some(options) = rule["anyOf"].as_array() {
        return options.iter().any(|r| valid(r, value, contract));
    }
    if let Some(options) = rule["enum"].as_array() {
        return options.contains(value);
    }
    let min = rule["min"].as_u64().unwrap_or(0);
    let max = rule["max"].as_u64().unwrap_or(1048576);
    match rule["type"].as_str().unwrap() {
        "null" => value.is_null(),
        "boolean" => value.is_boolean(),
        "integer" => value.as_u64().is_some_and(|v| v >= min && v <= max),
        "string" => value.as_str().is_some_and(|v| {
            if v.contains('\0')
                || (v.chars().count() as u64) < min
                || (v.chars().count() as u64) > max
            {
                return false;
            }
            if let Some(prefixes) = rule["id_prefixes"].as_array() {
                let Some((prefix, id)) = v.split_once('_') else {
                    return false;
                };
                if !prefixes.iter().any(|p| p.as_str() == Some(prefix)) || id.len() != 36 {
                    return false;
                }
                if !id.bytes().enumerate().all(|(i, c)| {
                    if [8, 13, 18, 23].contains(&i) {
                        c == b'-'
                    } else {
                        c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
                    }
                }) {
                    return false;
                }
            }
            if rule["format"] == "query"
                && (!v.chars().any(char::is_alphanumeric) || v.len() > 4096)
            {
                return false;
            }
            if rule["format"] == "timestamp"
                && (v.len() != 20
                    || !v.bytes().enumerate().all(|(i, c)| match i {
                        4 | 7 => c == b'-',
                        10 => c == b'T',
                        13 | 16 => c == b':',
                        19 => c == b'Z',
                        _ => c.is_ascii_digit(),
                    }))
            {
                return false;
            }
            true
        }),
        "array" => value.as_array().is_some_and(|items| {
            items.len() as u64 >= min
                && items.len() as u64 <= max
                && (rule["unique"] != true
                    || items
                        .iter()
                        .enumerate()
                        .all(|(i, v)| !items[..i].contains(v)))
                && items.iter().all(|v| valid(&rule["items"], v, contract))
        }),
        "object" => value.as_object().is_some_and(|obj| {
            let props = rule["properties"].as_object().unwrap();
            let optional = rule["optional"].as_array().unwrap();
            if !obj.keys().all(|k| props.contains_key(k))
                || !props
                    .keys()
                    .all(|k| obj.contains_key(k) || optional.contains(&Value::String(k.clone())))
            {
                return false;
            }
            if !obj.iter().all(|(k, v)| valid(&props[k], v, contract)) {
                return false;
            }
            if obj.contains_key("complete")
                && value["complete"].as_bool() != Some(value["next_cursor"].is_null())
            {
                return false;
            }
            if obj.contains_key("representative_capture_id")
                && value["representative_capture_id"] != value["capture_ids"][0]
            {
                return false;
            }
            if obj.contains_key("record_type") {
                let id = value["record_id"].as_str().unwrap();
                if value["record_type"] == "source" {
                    if !id.starts_with("capture_")
                        || value["record_id"] != value["revision_id"]
                        || value["source_id"].is_null()
                        || value["provenance"]["capture_ids"].as_array().unwrap().len() != 1
                        || value["provenance"]["capture_ids"][0] != value["record_id"]
                    {
                        return false;
                    }
                } else if !id.starts_with("page_")
                    || !value["revision_id"]
                        .as_str()
                        .unwrap()
                        .starts_with("revision_")
                    || !value["source_id"].is_null()
                {
                    return false;
                }
            }
            if obj.contains_key("content") {
                let length = value["content"]["text"].as_str().unwrap().len() as u64;
                let start = value["start_byte"].as_u64().unwrap();
                let end = value["end_byte"].as_u64().unwrap();
                if end.checked_sub(start) != Some(length)
                    || (value["complete"] == false && length == 0)
                {
                    return false;
                }
            }
            if obj.contains_key("required_grants")
                && (value["required_grants"].as_array().unwrap().len() != 1
                    || value["required_grants"][0]
                        != contract["grants"][value["name"].as_str().unwrap()])
            {
                return false;
            }
            if let Some(ops) = value["operations"].as_array() {
                if !ops
                    .iter()
                    .enumerate()
                    .all(|(i, v)| !ops[..i].iter().any(|old| old["name"] == v["name"]))
                {
                    return false;
                }
            }
            true
        }),
        _ => false,
    }
}
#[test]
fn t03_strict_raw_contract_corpus() {
    let contract: Value =
        serde_json::from_str(include_str!("strict-contract.schema.json")).unwrap();
    let corpus: Value = serde_json::from_str(include_str!("strict-cases.json")).unwrap();
    for case in corpus["cases"].as_array().unwrap() {
        let result = from_slice(case["raw"].as_str().unwrap().as_bytes()).is_ok_and(|value| {
            valid(
                &contract["definitions"][case["schema"].as_str().unwrap()],
                &value,
                &contract,
            )
        });
        assert_eq!(result, case["accept"].as_bool().unwrap(), "{}", case["id"]);
    }
    assert!(from_slice(b"{\"x\":\"\xff\"}").is_err());
    assert!(from_slice(br#"{"x":{"role":1,"role":2}}"#).is_err());
}

#[test]
fn t03_raw_byte_encodings_match_consumers() {
    let contract: Value =
        serde_json::from_str(include_str!("strict-contract.schema.json")).unwrap();
    let corpus: Value = serde_json::from_str(include_str!("raw-byte-cases.json")).unwrap();
    for case in corpus["cases"].as_array().unwrap() {
        let bytes: Vec<u8> = case["bytes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        let result = from_slice(&bytes).is_ok_and(|v| {
            valid(
                &contract["definitions"][case["schema"].as_str().unwrap()],
                &v,
                &contract,
            )
        });
        assert_eq!(result, case["accept"].as_bool().unwrap(), "{}", case["id"]);
    }
}
