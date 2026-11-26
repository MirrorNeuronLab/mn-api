//! The MCP API embeds JSON as text and publishes digests over the document bytes.
use serde::Serialize;
use serde_json::{Value, ser::Formatter};
use std::{
    fmt::Write as _,
    io::{self, Write},
};

struct Spaced;

impl Formatter for Spaced {
    fn begin_array_value<W: Write + ?Sized>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
    fn begin_object_key<W: Write + ?Sized>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        self.begin_array_value(w, first)
    }
    fn begin_object_value<W: Write + ?Sized>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
}

pub(super) fn text(value: &Value) -> String {
    let mut bytes = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, Spaced,
        ))
        .expect("JSON value written into memory");
    String::from_utf8(bytes).expect("serialized JSON is UTF-8")
}

pub(super) fn ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        if ch < '\u{7f}' {
            out.push(ch);
        } else {
            for unit in ch.encode_utf16(&mut [0; 2]) {
                write!(out, "\\u{unit:04x}").expect("write to string");
            }
        }
    }
    out
}

pub(super) fn document(mut value: Value) -> String {
    value.sort_all_objects();
    let pretty = serde_json::to_string_pretty(&value).expect("JSON value");
    ascii(&pretty) + "\n"
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn displayed_text_and_downloaded_files_preserve_their_wire_formats() {
        let value = json!({"b":[1,2],"a":"é"});
        assert_eq!(text(&value), r#"{"b": [1, 2], "a": "é"}"#);
        assert_eq!(
            document(value),
            "{\n  \"a\": \"\\u00e9\",\n  \"b\": [\n    1,\n    2\n  ]\n}\n"
        );
    }
}
