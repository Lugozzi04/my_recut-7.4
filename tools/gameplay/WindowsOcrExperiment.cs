// Optional calibration-only Windows.Media.Ocr experiment; not a runtime dependency.
using System;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Threading.Tasks;
using System.Diagnostics;
using System.Web.Script.Serialization;
using Windows.Media.Ocr;
using Windows.Storage;
using Windows.Graphics.Imaging;

public class Entry
{
    public string id;
    public string path;
}

public class Program
{
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            Run(args[0]).GetAwaiter().GetResult();
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 2;
        }
    }

    static string Clean(string value)
    {
        var result = new StringBuilder();
        foreach (char character in value)
            result.Append(char.IsControl(character) ? ' ' : character);
        return result.ToString();
    }

    static async Task Run(string manifest)
    {
        var json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024 };
        var rows = new List<object>();
        var engine = OcrEngine.TryCreateFromUserProfileLanguages();
        if (engine == null)
            throw new Exception("No installed OCR language");
        foreach (var item in json.Deserialize<Entry[]>(File.ReadAllText(manifest)))
        {
            var timer = Stopwatch.StartNew();
            try
            {
                var file = await System.WindowsRuntimeSystemExtensions.AsTask<StorageFile>(
                    StorageFile.GetFileFromPathAsync(item.path));
                using (var stream = await System.WindowsRuntimeSystemExtensions.AsTask<Windows.Storage.Streams.IRandomAccessStream>(
                    file.OpenAsync(FileAccessMode.Read)))
                {
                    var decoder = await System.WindowsRuntimeSystemExtensions.AsTask<BitmapDecoder>(
                        BitmapDecoder.CreateAsync(stream));
                    using (var bitmap = await System.WindowsRuntimeSystemExtensions.AsTask<SoftwareBitmap>(
                        decoder.GetSoftwareBitmapAsync()))
                    {
                        var result = await System.WindowsRuntimeSystemExtensions.AsTask<OcrResult>(
                            engine.RecognizeAsync(bitmap));
                        rows.Add(new { id = item.id, text = Clean(result.Text), seconds = timer.Elapsed.TotalSeconds, error = (string)null });
                    }
                }
            }
            catch (Exception error)
            {
                rows.Add(new { id = item.id, text = "", seconds = timer.Elapsed.TotalSeconds, error = error.Message });
            }
        }
        Console.WriteLine(json.Serialize(new { engine = "Windows.Media.Ocr", language = engine.RecognizerLanguage.LanguageTag, rows = rows }));
    }
}
