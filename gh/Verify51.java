import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Bookmark;
import ghidra.program.model.listing.CommentType;
import ghidra.program.model.listing.Listing;
import java.util.Iterator;

/** Prints persisted comments and bookmarks so a saved project can be checked without the GUI. */
public class Verify51 extends GhidraScript {
    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();

        Iterator<Bookmark> bi = currentProgram.getBookmarkManager().getBookmarksIterator();
        int n = 0;
        while (bi.hasNext()) {
            Bookmark b = bi.next();
            if (n < 8) {
                println("BOOKMARK " + b.getAddress() + " " + b.getCategory() + "/" + b.getType()
                    + " " + b.getComment());
            }
            n++;
        }
        println("BOOKMARKS " + n);

        AddressIterator ai = listing.getCommentAddressIterator(CommentType.PRE,
                currentProgram.getMemory(), true);
        int c = 0;
        while (ai.hasNext()) {
            Address a = ai.next();
            if (c < 8) {
                println("PRE_COMMENT " + a + " " + listing.getCodeUnitAt(a)
                        .getComment(CommentType.PRE));
            }
            c++;
        }
        println("PRE_COMMENTS " + c);
    }
}
