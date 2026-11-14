import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressOutOfBoundsException;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.symbol.SymbolTable;

import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;

/**
 * Apply one ASxxxx `.sym' symbol table - the file sdas8051 writes under -s, laid out by
 * lstsym() in sdas/asxxsrc/aslist.c - to the open program, with the rules the simulator
 * applies, so that the two tools can be held to the same answer for the same bytes.
 * t/symmaptest.py is that cross-tool gate; t/symtest.py pins the simulator side.
 *
 * The rules here are the simulator's, deliberately: the constants, the right-anchored
 * record parse and the section handling mirror read_sym_file() and its helpers in
 * core/sim.src/uc.cc, and symMemory() mirrors cl_51core::sym_memory() in
 * sims/s51.src/uc51.cc. Where the two must differ it is because Ghidra has no cell
 * concept - see the notes on the skip reasons below.
 *
 * The area field of a record is a hex area REFERENCE NUMBER (` %2X ' in lstsym()) and the
 * name is looked up in the Area Table rows that follow the symbols: `  %2X ' + `%-8.8s' +
 * `   size %4X   flags %4X\n' in the default hex, 2-byte-address, non-LONGINT build
 * (aslist.c:1268, :1278, :1319; octal/decimal xflag and -w builds differ) - same numbering, and
 * nothing here guesses one.  So an external-data symbol is one whose area is XSEG or XISEG
 * (sdld's AOMF51 names for external RAM, sdcc's back end spells it _XSEG, hence the
 * leading-underscore strip mirrored below), and CODE/EXTMEM are chosen for it exactly as
 * cl_51core::sym_memory() chooses rom/xram.  XRAM and XDATA in this language are two
 * memory BLOCKS of the single EXTMEM space, not two spaces: a symbol at 0x1234 is EXTMEM
 * just as one at 0x0100 is.
 *
 * Usage: -postScript LoadSym51.java [FILE.sym]
 * With no FILE it only dumps the symbol table twice, i.e. what a freshly imported image
 * knows before any symbol file exists, which is the control the gate needs.
 *
 * One line each, machine readable:
 *   SPACE <name> max=<address>                         address spaces of the language
 *   BLOCK <name> <start>..<end>                          memory blocks of the image
 *   SYMBEFORE <space>:<hex> <name> primary=<bool>        whole table, before the load
 *   SYM <space>:<hex> <name> primary=<bool>              one per record this file placed
 *   SKIP <name> line=<n> ref=<r> area=<a> value=<v> reason=<why>   one per refusal
 *   SYMALL <space>:<hex> <name> primary=<bool>           whole table, after the load
 *   LOAD file=<path> placed=<n> kept=<n> undefined=<n> unusable=<n> no_cell=<n>
 *        out_of_range=<n> relative=<n> error=<n> bad_line=<n|-> bad_why='...'
 *
 * Skips, all of them refusals rather than relocations: a bit (BSEG) symbol has no byte
 * cell to name, so it is reported and dropped, never parked at a plausible byte; an
 * undefined (`****') record has no address at all; a name the pspec already binds (the
 * SFR map) keeps its binding, because a `.sym' row states only what the assembler called
 * a thing, not what it is; and a record that cannot be parsed names its line and stops
 * the load, so half a table is never mistaken for the whole one.
 */
public class LoadSym51 extends GhidraScript {

    /* core/sim.src/uc.cc: sym_area_NR, sym_area_field, sym_area_ABS (A3_ABS of aslink.h) */
    private static final int AREA_NR = 256;
    private static final int AREA_FIELD = 4;
    private static final int AREA_ABS = 0x08;

    /* The simulator's memories, by the name this language gives the same storage.
     * Measured, not guessed: Blocks51 and Symbols51 dump CODE, INTMEM, SFR, EXTMEM, BITS. */
    private static final String CODE = "CODE";        // sim: rom
    private static final String INTMEM = "INTMEM";    // sim: iram
    private static final String SFR = "SFR";          // sim: sfr
    private static final String EXTMEM = "EXTMEM";    // sim: xram

    /* cl_51core::sym_memory(): areas whose value addresses data, not code */
    private static final String[] DATA_AREAS = {
        "DSEG", "PSEG", "XSEG", "XISEG", "ISEG", "SSEG", "OSEG", "DATA", "INITIALIZED",
        "REG_BANK", "GENOVER",
    };

    private static final int SEC_NONE = 0;
    private static final int SEC_SYMBOLS = 1;
    private static final int SEC_AREAS = 2;

    /** One row of the Symbol Table section; kept until the areas are known. */
    private static final class Row {
        String name;
        String value;
        long addr;
        int area = -1;
        boolean relocatable;
        boolean undefined;
        int line;
    }

    private final String[] areaName = new String[AREA_NR];
    private final boolean[] areaSeen = new boolean[AREA_NR];
    private final boolean[] areaAbsolute = new boolean[AREA_NR];
    private final List<Row> rows = new ArrayList<Row>();

    private int section = SEC_NONE;
    private int lineNr = 0;
    private int headerLeft = 0;
    private int badLine = 0;
    private String badWhy = "";

    private long nPlaced, nKept, nUndefined, nUnusable, nNoCell, nOutOfRange, nRelative, nError;

    @Override
    public void run() throws Exception {
        SymbolTable st = currentProgram.getSymbolTable();
        AddressSpace[] spaces = currentProgram.getAddressFactory().getAddressSpaces();
        for (int i = 0; i < spaces.length; i++) {
            println("SPACE " + spaces[i].getName() + " max=" + spaces[i].getMaxAddress());
        }
        MemoryBlock[] bs = currentProgram.getMemory().getBlocks();
        for (int i = 0; i < bs.length; i++) {
            println("BLOCK " + bs[i].getName() + " " + bs[i].getStart() + ".." + bs[i].getEnd());
        }
        Map<String, String> before = dump("SYMBEFORE", st);

        String[] args = getScriptArgs();
        String file = (args != null && args.length > 0) ? args[0] : null;
        boolean loaded = false;
        if (file == null) {
            println("LOAD file=none placed=0 kept=0 undefined=0 unusable=0 no_cell=0"
                    + " out_of_range=0 relative=0 error=0 bad_line=-");
        } else {
            try {
                readSymFile(file);
                bind(st, before);
                loaded = true;
            } catch (IOException e) {
                println("LOAD file=" + file + " error=cannot_open");
            }
        }
        dump("SYMALL", st);
        if (loaded) {
            summarize(file);
        }
    }

    /** The whole symbol table, one line per symbol; also its name -> address map. */
    private Map<String, String> dump(String tag, SymbolTable st) {
        Map<String, String> names = new HashMap<String, String>();
        SymbolIterator sit = st.getAllSymbols(true);
        while (sit.hasNext()) {
            Symbol s = sit.next();
            String where = addrString(s.getAddress());
            names.put(s.getName(), where);
            println(tag + " " + where + " " + s.getName() + " primary=" + s.isPrimary());
        }
        return names;
    }

    private String addrString(Address a) {
        return a.getAddressSpace().getName() + ":" + Long.toString(a.getOffset(), 16);
    }

    // --- the parser, mirroring sym_load_line/sym_load_symbol/sym_load_area --------------

    private void readSymFile(String path) throws IOException {
        BufferedReader in = new BufferedReader(new FileReader(path));
        try {
            String raw;
            while ((raw = in.readLine()) != null) {
                lineNr++;
                if (badLine != 0) {
                    continue;                       // a refused row ends the file
                }
                loadLine(raw);
            }
        } finally {
            in.close();
        }
    }

    private void loadLine(String rawLine) {
        /*
         * A `.sym' longer than one page repeats slew()'s four-line header at every page
         * break - a form feed with the version line ending `Page N', the radix line, a
         * timestamp, then the section title again. The version line is indistinguishable
         * from a record whose value is the page number, so without this the loader refused
         * everything past the first page break of any real build (t/map/tiny.sym from sdcc
         * 4.6.0 breaks on line 61). Mirrors sym_load_line() in core/sim.src/uc.cc; the
         * section title is deliberately not swallowed, since it re-selects the section.
         */
        if (headerLeft > 0) {
            headerLeft--;                             // the radix line, then the timestamp
            return;
        }
        if (rawLine.length() > 0 && rawLine.charAt(0) == '\f') {
            headerLeft = 2;
            return;
        }
        String line = rawLine.trim();
        if (line.length() == 0) {
            return;
        }
        if (isBanner(line)) {
            return;
        }
        if (isRadixLine(line)) {
            headerLeft = 1;                           // the timestamp slew() prints next
            return;
        }
        if (line.equals("Symbol Table")) {
            section = SEC_SYMBOLS;
            return;
        }
        if (line.equals("Area Table")) {
            section = SEC_AREAS;
            return;
        }
        if (section == SEC_NONE) {
            return;                                 // version lines and comments
        }
        if (section == SEC_AREAS) {
            loadArea(line);
            return;
        }
        // the record columns are fixed width and the first one is often blank, so the
        // line is split as it stands; only the headings were trimmed
        int start = 0;
        for (;;) {
            int bar = rawLine.indexOf('|', start);
            loadSymbol(bar < 0 ? rawLine.substring(start) : rawLine.substring(start, bar));
            if (bar < 0 || badLine != 0) {
                return;
            }
            start = bar + 1;
        }
    }

    private void loadSymbol(String seg) {
        Row row = new Row();
        String why = parseRow(seg, row);
        if (why != null) {
            if (why.length() > 0) {
                badLine = lineNr;
                badWhy = why;
            }
            return;
        }
        row.line = lineNr;
        if (row.undefined) {
            nUndefined++;
            skip(row, "", "undefined");
            return;
        }
        if (!validSymName(row.name)) {
            nUnusable++;
            skip(row, "", "unusable_name");
            return;
        }
        rows.add(row);
    }

    /**
     * Take one record apart working from the right: the value column is the only field
     * lstsym() keeps at a fixed width, while the name column is 8 wide or 60 wide under
     * -w and a longer name simply overflows it.
     *
     * @return null when the record parsed, "" when it is only the padding between two
     *         records, and the reason when it is something the assembler never printed.
     */
    private String parseRow(String seg, Row row) {
        String buf = rtrim(seg);
        int l = buf.length();
        if (l < 1) {
            return "";                              // the gap between two '|' records
        }
        int nflag = 0;
        while (nflag < l && isFlag(buf.charAt(l - 1 - nflag))) {
            nflag++;
        }
        for (int i = 0; i < nflag; i++) {
            char c = buf.charAt(l - 1 - i);
            if (c == 'R') {
                row.relocatable = true;
            }
            if (c == 'X') {
                row.undefined = true;
            }
        }
        l -= nflag;
        while (l > 0 && isBlank(buf.charAt(l - 1))) {
            l--;
        }
        int v = 0;
        while (v < l && (isHex(buf.charAt(l - 1 - v)) || buf.charAt(l - 1 - v) == '*')) {
            v++;
        }
        if (v == 0) {
            return "no address in the line";
        }
        String val = buf.substring(l - v, l);
        l -= v;
        while (l > 0 && isBlank(buf.charAt(l - 1))) {
            l--;
        }
        if (!isValueToken(val)) {
            return "'" + val + "' is not an address";
        }
        if (l > 0 && buf.charAt(l - 1) == '=') {
            l--;                                    // equate: '=' is not part of a name
        }
        if (l <= AREA_FIELD) {
            return "no name column";
        }
        String name = buf.substring(AREA_FIELD, l).trim();
        String ref = buf.substring(0, AREA_FIELD).trim();
        if (name.length() == 0) {
            return "name is empty";
        }
        row.name = name;
        row.value = val;
        if (ref.length() == 0) {
            row.area = -1;                          // `.equ' and absolute symbols
        } else {
            for (int i = 0; i < ref.length(); i++) {
                if (!isHex(ref.charAt(i))) {
                    return "area '" + ref + "' is not a hex area reference";
                }
            }
            row.area = (int) Long.parseLong(ref, 16);
        }
        if (val.charAt(0) == '*') {
            row.undefined = true;
        }
        row.addr = row.undefined ? 0 : Long.parseLong(val, 16);
        return null;
    }

    private void loadArea(String line) {
        String[] t = line.split("[ \t]+");
        String ref = t.length > 0 ? t[0] : "";
        String name = t.length > 1 ? t[1] : "";
        String kw1 = t.length > 2 ? t[2] : "";
        String kw2 = t.length > 4 ? t[4] : "";
        String flags = t.length > 5 ? t[5] : "";
        if (!kw1.equals("size") || !kw2.equals("flags") || ref.length() == 0
                || name.length() == 0) {
            badLine = lineNr;
            badWhy = "not an area row";
            return;
        }
        for (int i = 0; i < ref.length(); i++) {
            if (!isHex(ref.charAt(i))) {
                badLine = lineNr;
                badWhy = "area number '" + ref + "' is not hex";
                return;
            }
        }
        int nr = (int) Long.parseLong(ref, 16);
        if (nr >= AREA_NR) {
            return;
        }
        areaName[nr] = name;
        areaSeen[nr] = true;
        areaAbsolute[nr] = (hexOr0(flags) & AREA_ABS) != 0;
    }

    // --- the binder, mirroring cl_uc::read_sym_file ------------------------------------

    private void bind(SymbolTable st, Map<String, String> before) {
        Map<String, Symbol> mine = new HashMap<String, Symbol>();
        for (int i = 0; i < rows.size(); i++) {
            Row row = rows.get(i);
            String area = "";
            boolean relocatable = row.relocatable;
            if (row.area >= 0 && row.area < AREA_NR && areaSeen[row.area]) {
                area = areaName[row.area];
                if (areaAbsolute[row.area]) {
                    relocatable = false;            // absolute area: the value is final
                }
            }
            String space = symMemory(area, row.addr);
            if (space == null) {
                nNoCell++;
                skip(row, area, "no_byte_cell");
                continue;
            }
            AddressSpace as = space(space);
            if (as == null) {
                nNoCell++;
                skip(row, area, "no_such_space:" + space);
                continue;
            }
            Address at;
            try {
                at = as.getAddress(row.addr);
            } catch (AddressOutOfBoundsException e) {
                nOutOfRange++;
                skip(row, area, "out_of_range");
                continue;
            }
            if (before.containsKey(row.name)) {
                nKept++;
                skip(row, area, "already_bound");
                continue;
            }
            Symbol old = mine.get(row.name);
            if (old != null) {
                try {
                    old.delete();                   // a name this file bound is replaced
                } catch (Exception e) {
                    // the older cell stays; the newer one is refused below
                }
            }
            Symbol made;
            try {
                made = st.createLabel(at, row.name, SourceType.USER_DEFINED);
            } catch (Exception e) {
                nError++;
                skip(row, area, "cannot_name:" + e.getClass().getSimpleName());
                continue;
            }
            mine.put(row.name, made);
            nPlaced++;
            if (relocatable) {
                nRelative++;
            }
            println("SYM " + addrString(made.getAddress()) + " " + made.getName()
                    + " primary=" + made.isPrimary());
        }
    }

    /** What the run did, in one line: the counts the gate reads, and the refused row. */
    private void summarize(String file) {
        StringBuilder sb = new StringBuilder();
        sb.append("LOAD file=").append(file)
          .append(" placed=").append(nPlaced)
          .append(" kept=").append(nKept)
          .append(" undefined=").append(nUndefined)
          .append(" unusable=").append(nUnusable)
          .append(" no_cell=").append(nNoCell)
          .append(" out_of_range=").append(nOutOfRange)
          .append(" relative=").append(nRelative)
          .append(" error=").append(nError)
          .append(" bad_line=").append(badLine == 0 ? "-" : Integer.toString(badLine));
        if (badLine != 0) {
            sb.append(" bad_why='").append(badWhy).append("'");
        }
        println(sb.toString());
    }


    /**
     * Which address space the value of a symbol lives in - cl_51core::sym_memory().
     * The area name says code or data, and a data address says which of internal RAM,
     * the SFRs and external data was meant; a bit symbol says nothing, because its value
     * is a bit number rather than a byte address, so no byte cell stands for it and the
     * caller has to refuse the row instead of parking it on a plausible byte.
     */
    private String symMemory(String area, long addr) {
        boolean data = area == null || area.length() == 0;
        if (!data) {
            String name = area.toUpperCase();
            while (name.startsWith("_")) {
                name = name.substring(1);
            }
            if (name.indexOf("BSEG") >= 0) {
                return null;
            }
            /*
             * XSEG and XISEG name external RAM whatever the address says: sdcc links
             * XSEG at 0x0001 by default, so the first __xdata object sits at 0x0001,
             * where the ladder below answers INTMEM and puts the symbol in the wrong
             * memory.  This is the rule this class's own header states and the one
             * lkmem.c uses to size XRam; mirrors the same fix in
             * cl_51core::sym_memory().
             */
            if (name.indexOf("XSEG") >= 0 || name.indexOf("XISEG") >= 0) {
                AddressSpace ext = space(EXTMEM);
                return (ext != null && validAddress(ext, addr)) ? EXTMEM : null;
            }
            for (int i = 0; i < DATA_AREAS.length && !data; i++) {
                if (name.indexOf(DATA_AREAS[i]) >= 0) {
                    data = true;
                }
            }
            if (!data) {
                return CODE;
            }
        }
        if (addr <= 0x7f) {
            return INTMEM;
        }
        if (addr <= 0xff) {
            return SFR;
        }
        AddressSpace x = space(EXTMEM);
        if (x != null && validAddress(x, addr)) {
            return EXTMEM;
        }
        return CODE;                        // the simulator's `return rom' fallthrough
    }

    /** cl_memory::valid_address(): can this address space reach that number at all?
     *  (AddressSpace.getSize() is a bit width, 16 here, NOT a reach: comparing an
     *  address against it sent every 0x100+ data symbol to CODE.) */
    private boolean validAddress(AddressSpace sp, long addr) {
        try {
            sp.getAddress(addr);
            return true;
        } catch (AddressOutOfBoundsException e) {
            return false;
        }
    }

    private AddressSpace space(String name) {
        if (name == null) {
            return null;
        }
        try {
            return currentProgram.getAddressFactory().getAddressSpace(name);
        } catch (NoSuchElementException e) {
            return null;
        }
    }

    private void skip(Row row, String area, String why) {
        println("SKIP " + row.name + " line=" + row.line
                + " ref=" + (row.area < 0 ? "-" : Integer.toHexString(row.area).toUpperCase())
                + " area=" + (area == null || area.length() == 0 ? "absolute" : area)
                + " value=" + row.value + " reason=" + why);
    }

    // --- the character classes the simulator's parser uses ------------------------------

    private static boolean isFlag(char c) {
        return c == 'G' || c == 'L' || c == 'R' || c == 'X';
    }

    private static boolean isBanner(String line) {
        // the old ASxxxx shape (the `.map' banner); lstsym()'s real one starts with \f
        return line.length() > 0 && line.endsWith(".") && line.indexOf(", page ") >= 0;
    }

    /** The second line of the four-line page header slew() repeats; aslist.c picks
     *  among these three strings by xflag. */
    private static boolean isRadixLine(String line) {
        return line.startsWith("Hexadecimal [") || line.startsWith("Octal [")
                || line.startsWith("Decimal [");
    }

    private static boolean isValueToken(String tok) {
        int l = tok.length();
        if (l != 4 && l != 6 && l != 8) {
            return false;
        }
        boolean stars = tok.charAt(0) == '*';
        for (int i = 0; i < l; i++) {
            char c = tok.charAt(i);
            if ((c == '*') != stars) {
                return false;
            }
            if (!stars && !isHex(c)) {
                return false;
            }
        }
        return true;
    }

    /** valid_sym_name() in core/utils.src/utils.cc: a name the console could type. */
    private static boolean validSymName(String s) {
        if (s == null || s.length() == 0) {
            return false;
        }
        char c = s.charAt(0);
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_')) {
            return false;
        }
        for (int i = 1; i < s.length(); i++) {
            c = s.charAt(i);
            if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
                    || c == '_')) {
                return false;
            }
        }
        return true;
    }

    private static boolean isHex(char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    }

    private static boolean isBlank(char c) {
        return c == ' ' || c == '\t' || c == '\r' || c == '\f' || c == 0x0b;
    }

    private static String rtrim(String s) {
        int l = s.length();
        while (l > 0 && isBlank(s.charAt(l - 1))) {
            l--;
        }
        return s.substring(0, l);
    }

    private static long hexOr0(String s) {
        long v = 0;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            int d;
            if (c >= '0' && c <= '9') {
                d = c - '0';
            } else if (c >= 'a' && c <= 'f') {
                d = c - 'a' + 10;
            } else if (c >= 'A' && c <= 'F') {
                d = c - 'A' + 10;
            } else {
                break;
            }
            v = v * 16 + d;
        }
        return v;
    }
}
