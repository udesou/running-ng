(* adapter.ml — PRODUCER-SIDE legacy → contract shim (lives in running-ng).

   Built against the shared contract opam package `bench-contract` (module
   `Schema`). `adapter --schema-version` prints the contract version it was built
   against, so running-ng can warn when the installed bench-contract package is
   newer than the adapter (see contract-adapter/README.md).

   This is the ONE place that knows running-ng's legacy on-disk layout (filenames
   + raw olly_/perf_ sidecars). It converts a legacy run directory into
   data-contract artifacts (Schema.Contract) and writes them to an output dir:

     adapter <legacy-run-dir> <out-dir>
       -> <out-dir>/manifest.json       (Contract.manifest)
       -> <out-dir>/measurements.json   (Contract.measurement array)

   The ingestor and everything downstream consume ONLY these contract artifacts
   and never see the legacy layout. When running-ng emits contract artifacts
   natively (config schema_version set), this adapter is bypassed entirely; it
   remains only for legacy / archived runs (a sunset path).

   Output is stamped with provenance (manifest._produced_by) and validated before
   writing. *)

open Schema
module U = Yojson.Safe.Util

let adapter_version = "adapter 0.1 (from legacy)"

(* ------------------------------------------------------------------ *)
(* Small string helpers                                                 *)
(* ------------------------------------------------------------------ *)

let starts_with pfx s =
  String.length s >= String.length pfx && String.sub s 0 (String.length pfx) = pfx

let ends_with sfx s =
  let ls = String.length s and lf = String.length sfx in
  ls >= lf && String.sub s (ls - lf) lf = sfx

let strip_prefix pfx s = String.sub s (String.length pfx) (String.length s - String.length pfx)

let contains_sub s sub =
  let ls = String.length s and lb = String.length sub in
  let rec go i =
    if i + lb > ls then false
    else if String.sub s i lb = sub then true
    else go (i + 1)
  in
  lb = 0 || go 0

(* ------------------------------------------------------------------ *)
(* Filename metadata (the quarantined legacy knowledge)                 *)
(*   <bench>.<hfac>.<size>.<runtime…>.<mod…>.<suite>                     *)
(* ------------------------------------------------------------------ *)

let known_flag_suffixes = [ "fp-flambda"; "flambda"; "fp" ]

let split_ocaml s =
  let rec go = function
    | [] -> (s, "baseline")
    | suf :: rest ->
        let tail = "-" ^ suf in
        if ends_with tail s then (String.sub s 0 (String.length s - String.length tail), suf)
        else go rest
  in
  go known_flag_suffixes

let strip_runtime_prefix s =
  if starts_with "ocaml-" s then strip_prefix "ocaml-" s
  else if starts_with "oxcaml-" s then strip_prefix "oxcaml-" s
  else s

let infer_kind runtime_name =
  let s = String.lowercase_ascii runtime_name in
  if contains_sub s "oxcaml" then "OxCaml"
  else if contains_sub s "mmtk" then "OCamlMMTk"
  else "OCaml"

let option_of_flag = function "fp" -> "frame-pointers" | other -> other

let options_of_flags = function
  | "baseline" -> []
  | flags -> List.map option_of_flag (String.split_on_char '-' flags)

type meta = {
  benchmark : string;
  suite : string;
  runtime_name : string;
  version : string;
  options : string list;
  kind : string;
  dimensions : Contract.dimensions;
  modifiers : string list;
}

let dedup_keep_first (l : (string * 'a) list) : (string * 'a) list =
  let seen = Hashtbl.create 8 in
  List.filter
    (fun (k, _) -> if Hashtbl.mem seen k then false else (Hashtbl.add seen k (); true))
    l

let parse_meta base : meta option =
  match String.split_on_char '.' base with
  | bench :: _hfac :: _size :: rest when rest <> [] ->
      let arr = Array.of_list rest in
      let n = Array.length arr in
      let suite = arr.(n - 1) in
      let p = ref (-1) in
      Array.iteri (fun i t -> if !p < 0 && starts_with "perf_grp" t then p := i) arr;
      let runtime_hi = if !p >= 1 then !p else 1 in
      let runtime_name = String.concat "." (Array.to_list (Array.sub arr 0 runtime_hi)) in
      let mod_tokens =
        if n - 1 - runtime_hi > 0 then Array.to_list (Array.sub arr runtime_hi (n - 1 - runtime_hi))
        else []
      in
      let version, flags = split_ocaml (strip_runtime_prefix runtime_name) in
      let dims =
        List.filter_map
          (fun tok ->
            match String.index_opt tok '-' with
            | Some i ->
                let k = String.sub tok 0 i in
                let v = String.sub tok (i + 1) (String.length tok - i - 1) in
                if k = "re_par" || k = "md_par" then None
                else (
                  match List.assoc_opt k Registry.dimension_of_modifier with
                  | Some (dim, _unit) ->
                      let jv =
                        match int_of_string_opt v with Some i -> `Int i | None -> `String v
                      in
                      Some (dim, jv)
                  | None -> None)
            | None -> None)
          mod_tokens
        |> dedup_keep_first
      in
      Some
        {
          benchmark = bench;
          suite;
          runtime_name;
          version;
          options = options_of_flags flags;
          kind = infer_kind runtime_name;
          dimensions = dims;
          modifiers = mod_tokens;
        }
  | _ -> None

(* ------------------------------------------------------------------ *)
(* runbms.yml — the authoritative run metadata running-ng writes.       *)
(* Runtime identity (kind/version/commit/options) comes from here, keyed *)
(* by the runtime NAME in the filename; the filename is only the join    *)
(* key. Options are the raw configure_args — the same source a native    *)
(* runner reads — so config_id matches across adapter and native.        *)
(* ------------------------------------------------------------------ *)

type rt_identity = { rk : string; rv : string option; rc : string option; ro : string list }

let read_file path =
  let ic = open_in_bin path in
  let n = in_channel_length ic in
  let s = really_input_string ic n in
  close_in ic;
  s

let read_runtimes dir : (string, rt_identity) Hashtbl.t =
  let tbl = Hashtbl.create 16 in
  let add_runtimes runtimes =
    List.iter
      (fun (name, spec) ->
        match spec with
        | `O fields ->
            let s k = match List.assoc_opt k fields with Some (`String v) -> Some v | _ -> None in
            let opts =
              match List.assoc_opt "configure_args" fields with
              | Some (`A l) -> List.filter_map (function `String s -> Some s | _ -> None) l
              | _ -> []
            in
            Hashtbl.replace tbl name
              { rk = (match s "type" with Some t -> t | None -> "OCaml"); rv = s "version"; rc = s "commit"; ro = opts }
        | _ -> ())
      runtimes
  in
  let path = Filename.concat dir "runbms.yml" in
  if Sys.file_exists path then begin
    match Yaml.of_string (read_file path) with
    | Ok (`O top) -> (
        match List.assoc_opt "runtimes" top with Some (`O r) -> add_runtimes r | _ -> ())
    | Ok _ -> ()
    | Error (`Msg e) ->
        Printf.eprintf
          "WARN: could not parse %s (%s); falling back to filename identity. \
           Use `running adapt` (resolves YAML anchors via PyYAML) for authoritative identity.\n%!"
          path e
  end;
  tbl

(* Runtimes provided as JSON (e.g. by `running adapt`, which resolves runbms.yml's
   YAML anchors/merges via PyYAML). Same shape as runbms.yml's `runtimes:` block. *)
let runtimes_from_json path : (string, rt_identity) Hashtbl.t =
  let tbl = Hashtbl.create 16 in
  (try
     match Yojson.Safe.from_file path with
     | `Assoc runtimes ->
         List.iter
           (fun (name, spec) ->
             match spec with
             | `Assoc fields ->
                 let s k = match List.assoc_opt k fields with Some (`String v) -> Some v | _ -> None in
                 let opts =
                   match List.assoc_opt "configure_args" fields with
                   | Some (`List l) -> List.filter_map (function `String s -> Some s | _ -> None) l
                   | _ -> []
                 in
                 Hashtbl.replace tbl name
                   { rk = (match s "type" with Some t -> t | None -> "OCaml"); rv = s "version"; rc = s "commit"; ro = opts }
             | _ -> ())
           runtimes
     | _ -> ()
   with e -> Printf.eprintf "WARN: could not read runtimes json %s: %s\n%!" path (Printexc.to_string e));
  tbl

(* ------------------------------------------------------------------ *)
(* Comparisons — mirror the native (Python) mapping: running-ng's a/b   *)
(* blocks -> contract inter-runtime comparisons, so adapted-legacy and   *)
(* native manifests carry the same comparisons.                          *)
(* ------------------------------------------------------------------ *)

let rec yaml_to_json (y : Yaml.value) : Yojson.Safe.t =
  match y with
  | `Null -> `Null
  | `Bool b -> `Bool b
  | `Float f -> `Float f
  | `String s -> `String s
  | `A l -> `List (List.map yaml_to_json l)
  | `O l -> `Assoc (List.map (fun (k, v) -> (k, yaml_to_json v)) l)

(* comparison blocks from a JSON file (passed by `running adapt`, PyYAML-resolved) *)
let comparisons_from_json path : Yojson.Safe.t list =
  try match Yojson.Safe.from_file path with `List l -> l | _ -> [] with _ -> []

(* comparison blocks straight from runbms.yml (direct bin/adapter, anchor-free) *)
let comparisons_from_yaml dir : Yojson.Safe.t list =
  let path = Filename.concat dir "runbms.yml" in
  if not (Sys.file_exists path) then []
  else
    match Yaml.of_string (read_file path) with
    | Ok (`O top) -> (
        match List.assoc_opt "comparisons" top with Some (`A l) -> List.map yaml_to_json l | _ -> [])
    | _ -> []

let runtime_selector (rts : (string, rt_identity) Hashtbl.t) name : Contract.selector =
  match Hashtbl.find_opt rts name with
  | Some id ->
      [ ("runtime.version", `String (match id.rv with Some v -> v | None -> name)) ]
      @ (if id.ro <> [] then [ ("runtime.options", `List (List.map (fun o -> `String o) id.ro)) ] else [])
      @ (match id.rc with Some c -> [ ("runtime.commit", `String c) ] | None -> [])
  | None -> [ ("runtime.version", `String name) ]

let comp_names = function
  | `String s -> [ s ]
  | `List l -> List.filter_map (function `String s -> Some s | _ -> None) l
  | _ -> []

let map_comparisons (blocks : Yojson.Safe.t list) rts : Contract.comparison list =
  List.concat_map
    (fun block ->
      match block with
      | `Assoc fields -> (
          match (List.assoc_opt "a" fields, List.assoc_opt "b" fields) with
          | Some a, Some b ->
              let mode = match List.assoc_opt "mode" fields with Some (`String m) -> Some m | _ -> Some "pairwise" in
              let label = match List.assoc_opt "label" fields with Some (`String l) -> Some l | _ -> None in
              List.map
                (fun base ->
                  Contract.
                    {
                      kind = "inter";
                      label;
                      over = Some (`String "runtime");
                      mode;
                      baseline = Some (runtime_selector rts base);
                      variants = Some (List.map (runtime_selector rts) (comp_names b));
                      fix = None;
                      baseline_at = None;
                    })
                (comp_names a)
          | _ -> [])
      | _ -> [])
    blocks

let config_of_meta (rts : (string, rt_identity) Hashtbl.t) (m : meta) : Contract.config_descriptor =
  (* prefer authoritative identity from runbms.yml; fall back to filename parse *)
  let kind, version, commit, options =
    match Hashtbl.find_opt rts m.runtime_name with
    | Some id -> (id.rk, (match id.rv with Some v -> v | None -> m.version), id.rc, id.ro)
    | None -> (m.kind, m.version, None, m.options)
  in
  let runtime = Contract.{ kind; version; commit; options } in
  {
    config_id = Registry.canonical_config_id runtime m.dimensions;
    runtime;
    dimensions = m.dimensions;
    tools = [ "perf"; "olly" ];
    runtime_name = Some m.runtime_name;
    modifiers = m.modifiers;
  }

(* ------------------------------------------------------------------ *)
(* Sidecar reading                                                      *)
(* ------------------------------------------------------------------ *)

(* Reads plain or gzipped NDJSON. .gz is decompressed via `gunzip -c` (avoids a
   zlib/camlzip dependency); other people's logs may ship compressed. *)
let read_lines path =
  let gz = ends_with ".gz" path in
  let ic = if gz then Unix.open_process_in (Printf.sprintf "gunzip -c -- %s" (Filename.quote path)) else open_in path in
  let close () = if gz then ignore (Unix.close_process_in ic) else close_in ic in
  let rec loop acc =
    match input_line ic with
    | line -> loop (if String.trim line = "" then acc else line :: acc)
    | exception End_of_file -> close (); List.rev acc
  in
  loop []

let json_lines path =
  try List.filter_map (fun l -> try Some (Yojson.Safe.from_string l) with _ -> None) (read_lines path)
  with _ -> []

let fnum = function
  | `Int i -> Some (float_of_int i)
  | `Float f -> Some f
  | `Intlit s | `String s -> (try Some (float_of_string s) with _ -> None)
  | _ -> None

let get j k = try U.member k j with _ -> `Null

let rec dotted j = function
  | [] -> Some j
  | k :: rest -> (
      match j with
      | `Assoc l -> ( match List.assoc_opt k l with Some v -> dotted v rest | None -> None)
      | _ -> None)

let path_num j key = match dotted j (String.split_on_char '.' key) with Some v -> fnum v | None -> None

(* ------------------------------------------------------------------ *)
(* Metric normalization (via the registry)                              *)
(* ------------------------------------------------------------------ *)

let make_metric name value : Contract.metric option =
  match List.assoc_opt name Registry.metric_catalog with
  | Some (unit_, layer, source) -> Some Contract.{ name; value; unit_; source; layer }
  | None -> None

let warned = Hashtbl.create 4

let olly_version_ok olly =
  match get olly "version" with
  | `Int v ->
      if Registry.olly_output_version_supported |> List.mem v then true
      else begin
        if not (Hashtbl.mem warned v) then begin
          Hashtbl.add warned v ();
          Printf.eprintf
            "WARN: olly output version %d not in supported set %s — metrics may be misparsed (registry.ml)\n%!"
            v
            (String.concat "," (List.map string_of_int Registry.olly_output_version_supported))
        end;
        false
      end
  | _ -> true

let olly_metrics olly =
  let _ = olly_version_ok olly in
  List.filter_map
    (fun (path, name) -> match path_num olly path with Some v -> make_metric name v | None -> None)
    Registry.olly_field_map

let perf_metrics perf =
  match perf with
  | `List entries ->
      List.filter_map
        (fun e ->
          match get e "event" with
          | `String ev -> (
              match List.assoc_opt ev Registry.perf_event_map with
              | Some name -> ( match fnum (get e "counter-value") with Some v -> make_metric name v | None -> None)
              | None -> None)
          | _ -> None)
        entries
  | _ -> []

(* ------------------------------------------------------------------ *)
(* Load a legacy run dir -> measurements + distinct configs/benchmarks *)
(* ------------------------------------------------------------------ *)

let run_id_of_dir dir =
  Filename.basename (if ends_with "/" dir then String.sub dir 0 (String.length dir - 1) else dir)

let load ?runtimes ?comparison_blocks dir =
  let run_id = run_id_of_dir dir in
  let rts = match runtimes with Some m -> m | None -> read_runtimes dir in
  let comparisons =
    let blocks = match comparison_blocks with Some b -> b | None -> comparisons_from_yaml dir in
    map_comparisons blocks rts
  in
  let files = Sys.readdir dir |> Array.to_list in
  let ollys =
    List.filter
      (fun f -> starts_with "olly_" f && (ends_with ".json" f || ends_with ".json.gz" f))
      files
  in
  let measurements = ref [] in
  let configs = Hashtbl.create 16 in
  let benches = Hashtbl.create 16 in
  List.iter
    (fun olly_file ->
      let base = strip_prefix "olly_" olly_file in
      let base =
        if ends_with ".json.gz" base then String.sub base 0 (String.length base - String.length ".json.gz")
        else String.sub base 0 (String.length base - String.length ".json")
      in
      match parse_meta base with
      | None ->
          (* fail loud: a sidecar we can't place is reported, never silently dropped *)
          Printf.eprintf "WARN: skipping %s — filename did not parse to contract metadata\n%!" olly_file
      | Some meta ->
          let cfg = config_of_meta rts meta in
          if not (Hashtbl.mem configs cfg.config_id) then Hashtbl.add configs cfg.config_id cfg;
          let bkey = meta.benchmark ^ "\x00" ^ meta.suite in
          if not (Hashtbl.mem benches bkey) then
            Hashtbl.add benches bkey Contract.{ name = meta.benchmark; suite = meta.suite; tags = [] };
          let olly_recs = json_lines (Filename.concat dir olly_file) in
          (* perf sidecar may be plain or gzipped *)
          let perf_json = "perf_" ^ base ^ ".json" in
          let perf_gz = perf_json ^ ".gz" in
          let perf_file =
            if List.mem perf_json files then Some perf_json
            else if List.mem perf_gz files then Some perf_gz
            else None
          in
          let perf_recs =
            match perf_file with Some pf -> json_lines (Filename.concat dir pf) | None -> []
          in
          let n =
            if perf_recs = [] then List.length olly_recs
            else min (List.length olly_recs) (List.length perf_recs)
          in
          for i = 0 to n - 1 do
            let olly = List.nth olly_recs i in
            let perf = if i < List.length perf_recs then List.nth perf_recs i else `Null in
            let m =
              Contract.
                {
                  schema_version = Contract.schema_version;
                  run_id;
                  benchmark = { name = meta.benchmark; suite = meta.suite; tags = [] };
                  config = { config_id = cfg.config_id };
                  invocation = i;
                  metrics = olly_metrics olly @ perf_metrics perf;
                  raw_ref =
                    ("olly", Printf.sprintf "%s#L%d" olly_file (i + 1))
                    :: (match perf_file with
                       | Some pf -> [ ("perf", Printf.sprintf "%s#L%d" pf (i + 1)) ]
                       | None -> []);
                }
            in
            measurements := m :: !measurements
          done)
    ollys;
  let measurements = List.rev !measurements in
  let configs = Hashtbl.fold (fun _ v acc -> v :: acc) configs [] in
  let benches = Hashtbl.fold (fun _ v acc -> v :: acc) benches [] in
  (run_id, measurements, configs, benches, comparisons)

(* ------------------------------------------------------------------ *)
(* Manifest                                                             *)
(* ------------------------------------------------------------------ *)

let iso_of_run_id run_id =
  match String.split_on_char '-' run_id with
  | _host :: y :: mo :: d :: _day :: hms :: _ when String.length hms = 6 -> (
      try
        Printf.sprintf "%s-%s-%sT%s:%s:%sZ" y mo d (String.sub hms 0 2) (String.sub hms 2 2)
          (String.sub hms 4 2)
      with _ -> run_id)
  | _ -> run_id

let host_of_run_id run_id =
  match String.split_on_char '-' run_id with h :: _ -> h | [] -> run_id

let build_manifest run_id configs benches comparisons : Contract.manifest =
  {
    schema_version = Contract.schema_version;
    run_id;
    created_at = iso_of_run_id run_id;
    machine =
      {
        hostname = host_of_run_id run_id;
        cpu_model = None; cores = None; kernel = None; governor = None; isolcpus = None; turbo = None;
      };
    tool_versions = [];
    configs;
    comparisons;
    benchmarks = benches;
    produced_by = Some adapter_version;
  }

(* ------------------------------------------------------------------ *)
(* Validate-before-write (output is valid by construction, but this     *)
(* catches adapter bugs) and write the contract artifacts.              *)
(* ------------------------------------------------------------------ *)

let rec mkdir_p dir =
  if dir <> "" && dir <> "." && dir <> "/" && not (Sys.file_exists dir) then begin
    mkdir_p (Filename.dirname dir);
    (try Unix.mkdir dir 0o755 with Unix.Unix_error (Unix.EEXIST, _, _) -> ())
  end

let write_json path json =
  let oc = open_out path in
  output_string oc (Yojson.Safe.pretty_to_string json);
  output_char oc '\n';
  close_out oc

(* Split a combined measurement into a per-tool partial: only that tool's metrics
   and raw_ref. olly and perf thus land in separate NDJSON files, and the ingestor
   merges them back by identity — matching how a native runner (where the tools
   finish independently) would emit. *)
let partial_for_tool (m : Contract.measurement) (tool : string) : Contract.measurement option =
  let metrics = List.filter (fun (x : Contract.metric) -> x.source = tool) m.metrics in
  let raw_ref = List.filter (fun (k, _) -> k = tool) m.raw_ref in
  if metrics = [] && raw_ref = [] then None else Some { m with metrics; raw_ref }

let run_adapt ?runtimes ?comparison_blocks legacy_dir out_dir =
      let run_id, ms, cfgs, benches, comparisons = load ?runtimes ?comparison_blocks legacy_dir in
      let man = build_manifest run_id cfgs benches comparisons in
      (* self-validate *)
      let bad = ref 0 in
      List.iter
        (fun m ->
          match Contract.measurement_of_yojson (Contract.measurement_to_yojson m) with
          | Ok _ -> () | Error e -> incr bad; Printf.eprintf "INVALID measurement: %s\n%!" e)
        ms;
      (match Contract.manifest_of_yojson (Contract.manifest_to_yojson man) with
       | Ok _ -> () | Error e -> incr bad; Printf.eprintf "INVALID manifest: %s\n%!" e);
      if !bad > 0 then (Printf.eprintf "FATAL: %d invalid artifact(s); not writing\n%!" !bad; exit 1);
      mkdir_p out_dir;
      write_json (Filename.concat out_dir "manifest.json") (Contract.manifest_to_yojson man);
      (* per-tool NDJSON under measurements/ ; one buffer per tool, written once *)
      let mdir = Filename.concat out_dir "measurements" in
      mkdir_p mdir;
      let tools = [ "olly"; "perf" ] in
      let bufs = Hashtbl.create 4 in
      let buf tool =
        match Hashtbl.find_opt bufs tool with
        | Some b -> b
        | None -> let b = Buffer.create 65536 in Hashtbl.add bufs tool b; b
      in
      List.iter
        (fun m ->
          List.iter
            (fun tool ->
              match partial_for_tool m tool with
              | None -> ()
              | Some p ->
                  Buffer.add_string (buf tool) (Yojson.Safe.to_string (Contract.measurement_to_yojson p));
                  Buffer.add_char (buf tool) '\n')
            tools)
        ms;
      List.iter
        (fun tool ->
          match Hashtbl.find_opt bufs tool with
          | None -> ()
          | Some b ->
              let oc = open_out (Filename.concat mdir (tool ^ ".ndjson")) in
              Buffer.output_buffer oc b;
              close_out oc)
        tools;
      Printf.eprintf "adapter: wrote %d measurements as per-tool NDJSON + manifest for run %s to %s\n%!"
        (List.length ms) run_id out_dir

let usage () =
  prerr_endline "usage: adapter <legacy-run-dir> <out-dir> [--runtimes <json>] [--comparisons <json>]";
  prerr_endline "       adapter --schema-version";
  exit 2

let () =
  let argv = Sys.argv in
  match argv with
  | [| _; "--schema-version" |] ->
      (* the contract version this adapter was built against; running-ng compares
         it to the installed bench-contract package to detect an outdated adapter *)
      print_endline Contract.schema_version
  | _ ->
      let legacy = ref None and out = ref None and runtimes = ref None and comparisons = ref None in
      let i = ref 1 in
      while !i < Array.length argv do
        (match argv.(!i) with
         | "--runtimes" when !i + 1 < Array.length argv ->
             runtimes := Some (runtimes_from_json argv.(!i + 1));
             incr i
         | "--comparisons" when !i + 1 < Array.length argv ->
             comparisons := Some (comparisons_from_json argv.(!i + 1));
             incr i
         | s when !legacy = None -> legacy := Some s
         | s when !out = None -> out := Some s
         | s -> Printf.eprintf "adapter: unexpected argument %s\n%!" s);
        incr i
      done;
      (match (!legacy, !out) with
       | Some legacy_dir, Some out_dir ->
           run_adapt ?runtimes:!runtimes ?comparison_blocks:!comparisons legacy_dir out_dir
       | _ -> usage ())
