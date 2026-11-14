import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.BookmarkManager;
import ghidra.program.model.listing.CommentType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.scalar.Scalar;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import ghidra.program.model.symbol.ReferenceManager;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Reports which AT89C51RD2 register accesses the loaded language actually models, so the analyst
 * knows where decompiler output can be trusted.
 *
 * Under the stock "8051:BE:16:default" every AUXR1 write is a hazard: the language has no
 * DUAL_DPTR model, so DPTR0 and DPTR1 collapse into one variable and a block transfer between two
 * pointers decompiles to a self-referential copy.
 *
 * Under "AT89C51RD2:BE:16:default" (~/s51/lang) the bank is real context, and the two forms the
 * datasheet uses - MOV AUXR1,#imm and INC AUXR1 - are modelled, so only the remaining write forms
 * are hazards. Reads of AUXR1 are never a hazard.
 *
 * AUXR (0x8e) and CKCON0 (0x8f) are named correctly by the RD2 language but their effects - the
 * on-chip MOVX window and the clock mode - are not modelled by either language, so they stay
 * hazards in both.
 *
 * AUXR1 is not bit-addressable (0xa2 is not a multiple of 8), so a bit-space reference can never
 * be DPS traffic and bit space is deliberately not scanned.
 */
public class Dptr51 extends GhidraScript {

    private static final long[] OFF = { 0xa2L, 0x8eL, 0x8fL };
    private static final String[] NAME = {
        "AUXR1 (bit0 = DPS dual-DPTR select)",
        "AUXR (EXTRAM + XRS2:0 XRAM window)",
        "CKCON0 (X2 + timer/UART clock)" };
    private static final String[] WHY = {
        "dual-DPTR bank switch: DPTR0/DPTR1 alias in this language",
        "on-chip XRAM window change: MOVX targets before and after this differ",
        "clock mode change: affects every machine-cycle count" };

    private boolean rd2;

    @Override
    public void run() throws Exception {
        AddressSpace sfr = currentProgram.getAddressFactory().getAddressSpace("SFR");
        if (sfr == null) {
            println("DPTR51 no SFR address space - wrong language?");
            return;
        }
        String lang = currentProgram.getLanguage().getLanguageID().getIdAsString();
        rd2 = lang.startsWith("AT89C51RD2");
        println("DPTR51 language " + lang + " dual_dptr=" + (rd2 ? "modelled" : "absent"));

        // -noanalysis leaves the listing empty, so make sure something is disassembled. With a
        // full analysis pass this is already true and every block is skipped.
        Listing listing = currentProgram.getListing();
        MemoryBlock[] blocks = currentProgram.getMemory().getBlocks();
        int disassembled = 0;
        for (int b = 0; b < blocks.length; b++) {
            if (!blocks[b].isExecute() || !blocks[b].isInitialized()) {
                continue;
            }
            if (listing.getInstructionAt(blocks[b].getStart()) == null) {
                runCommand(new DisassembleCommand(blocks[b].getStart(), null, true));
                disassembled++;
            }
        }

        ReferenceManager refs = currentProgram.getReferenceManager();
        FunctionManager fns = currentProgram.getFunctionManager();
        BookmarkManager marks = currentProgram.getBookmarkManager();

        // the 8051 analyzer leaves the reset vector unfunctioned, which would hide which routine
        // each hazard belongs to. On this target code starts at the beginning of the ROM block.
        int functions = 0;
        for (int b = 0; b < blocks.length; b++) {
            if (blocks[b].isExecute() && blocks[b].isInitialized()
                    && fns.getFunctionContaining(blocks[b].getStart()) == null
                    && listing.getInstructionAt(blocks[b].getStart()) != null
                    && runCommand(new CreateFunctionCmd(blocks[b].getStart()))) {
                functions++;
            }
        }

        int total = 0;
        int modelled = 0;
        for (int k = 0; k < OFF.length; k++) {
            Address target = sfr.getAddress(OFF[k]);
            Map<String, Integer> perFn = new LinkedHashMap<String, Integer>();
            int sites = 0;
            int done = 0;

            ReferenceIterator ri = refs.getReferencesTo(target);
            while (ri.hasNext() && !monitor.isCancelled()) {
                Reference r = ri.next();
                int[] res = flag(listing, fns, marks, r.getFromAddress(), k, perFn,
                        r.getReferenceType().isRead(), r.getReferenceType().isWrite());
                sites += res[0];
                done += res[1];
            }

            // direct-mode operands are not always turned into references; catch those by operand
            if (sites == 0) {
                InstructionIterator it = listing.getInstructions(true);
                while (it.hasNext() && !monitor.isCancelled()) {
                    Instruction ins = it.next();
                    boolean hit = false;
                    for (int o = 0; o < ins.getNumOperands() && !hit; o++) {
                        Object[] vals = ins.getOpObjects(o);
                        for (int v = 0; v < vals.length; v++) {
                            if (vals[v] instanceof Address) {
                                Address a = (Address) vals[v];
                                if (a.getAddressSpace() == sfr && a.getOffset() == OFF[k]) {
                                    hit = true;
                                    break;
                                }
                            }
                        }
                    }
                    if (hit) {
                        // no reference, so assume it writes; a false positive is safer here
                        int[] res = flag(listing, fns, marks, ins.getAddress(), k, perFn,
                                false, true);
                        sites += res[0];
                        done += res[1];
                    }
                }
            }

            println("HAZARD " + NAME[k] + " @ SFR:" + Long.toHexString(OFF[k]) + " sites=" + sites
                + " modelled=" + done + " unmodelled=" + (sites - done));
            for (Map.Entry<String, Integer> e : perFn.entrySet()) {
                println("  FN " + e.getKey() + " touches=" + e.getValue());
            }
            total += sites - done;
            modelled += done;
        }
        println("DPTR51 unmodelled_sites " + total + " modelled_sites " + modelled
            + " blocks_disassembled " + disassembled + " functions_created " + functions);
        if (total == 0) {
            println("DPTR51 nothing unmodelled - stock decompiler output trustworthy");
        }
    }

    /** Returns {counted, modelled}: both 0 when the site is not a hazard at all. */
    private int[] flag(Listing listing, FunctionManager fns, BookmarkManager marks, Address where,
            int k, Map<String, Integer> perFn, boolean read, boolean write) {
        Instruction in = listing.getInstructionAt(where);
        if (in == null) {
            return new int[] { 0, 0 };
        }
        // reading AUXR1 tells you the bank; it cannot mislead the pointer analysis
        if (k == 0 && read && !write) {
            return new int[] { 0, 0 };
        }
        boolean ok = k == 0 && rd2 && write && modelledWrite(in);
        if (!ok) {
            Function f = fns.getFunctionContaining(where);
            String key = f == null ? "<outside function>" : f.getName();
            Integer c = perFn.get(key);
            perFn.put(key, c == null ? 1 : c.intValue() + 1);
            String note = "RD2 HAZARD " + NAME[k] + ": " + WHY[k];
            if (k == 1) {
                // the window is fully determined by an immediate, so say what it selects rather
                // than just warning that something changed
                String w = window(in);
                note += w == null ? " - window depends on a runtime value" : " - " + w;
            }
            note += " - verify against ucsim trace (t/uctrace.py"
                + (k == 0 ? ", t/dpstest.py, t/rmwtest.py" : ", t/xramtest.py") + ")";
            listing.setComment(where, CommentType.PRE, note);
            marks.setBookmark(where, "analysis", "rd2-hazard", note);
            println("  " + where + "  " + key + "  " + in.toString()
                + (k == 1 ? "   [" + (window(in) == null ? "dynamic window" : window(in)) + "]"
                          : ""));
        }
        return new int[] { 1, ok ? 1 : 0 };
    }

    /**
     * Every form the RD2 language resolves the bank for. doc4235's idiom is MOV AUXR1,#imm and
     * INC AUXR1, but the immediate-taking RMW forms are statically resolvable too: ORL forces the
     * bank when bit 0 is set, ANL clears it when bit 0 is clear, XRL toggles when bit 0 is set,
     * and DEC always toggles because bit 0 flips on a decrement for the same borrow reason that
     * makes INC toggle it. t/rmwtest.py checks all of these against the executing simulator.
     * Left out: MOV from A/Rn/@Ri/direct, whose value is not knowable while parsing. There is no
     * CPL direct on the 8051, so that form does not exist.
     */
    private boolean modelledWrite(Instruction in) {
        String m = in.getMnemonicString();
        if (m.equalsIgnoreCase("INC") || m.equalsIgnoreCase("DEC")
                || m.equalsIgnoreCase("DJNZ")) {
            return in.getNumOperands() == (m.equalsIgnoreCase("DJNZ") ? 2 : 1);
        }
        if (m.equalsIgnoreCase("MOV") || m.equalsIgnoreCase("ORL")
                || m.equalsIgnoreCase("ANL") || m.equalsIgnoreCase("XRL")) {
            Object[] v = in.getNumOperands() == 2 ? in.getOpObjects(1) : new Object[0];
            return v.length == 1 && v[0] instanceof Scalar;
        }
        return false;
    }

    /** doc4235 Table 9-1: XRS2:0 selects the on-chip XRAM size; 101..111 are undocumented. */
    private static final int[] XRAM_SIZE = { 256, 512, 768, 1024, 1792, 1792, 1792, 1792 };

    /** The window an AUXR write selects, or null when the value is not a constant. */
    private String window(Instruction in) {
        if (!in.getMnemonicString().equalsIgnoreCase("MOV") || in.getNumOperands() != 2) {
            return null;
        }
        Object[] v = in.getOpObjects(1);
        if (v.length != 1 || !(v[0] instanceof Scalar)) {
            return null;
        }
        long auxr = ((Scalar) v[0]).getUnsignedValue();
        if ((auxr & 0x02) != 0) {
            return "EXTRAM=1, every MOVX is off-chip";
        }
        int size = XRAM_SIZE[(int) ((auxr >> 2) & 7)];
        return "EXTRAM=0, on-chip XRAM 0x0000..0x" + Long.toHexString(size - 1)
            + " (" + size + " bytes), above that off-chip";
    }
}
