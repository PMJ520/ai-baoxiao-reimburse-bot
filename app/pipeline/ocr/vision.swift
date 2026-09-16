// 用 macOS Vision 识别票据/账单截图。
// 用法：ocr <图片1> [图片2 ...]
// 输出：每张图以 "===FILE\t<路径>" 开头，随后每行为
//       文本 \t x \t y \t 宽 \t 高   （均为 0~1 归一化，原点在左上）
// 输出坐标是为了按位置配对「标签 → 值」——账单页里两者同处一行，
// 而 Vision 的返回顺序是先一列标签、后一列值，仅靠行序会错配。
import Foundation
import Vision
import AppKit

let files = Array(CommandLine.arguments.dropFirst())
guard !files.isEmpty else {
    FileHandle.standardError.write("用法: ocr <图片...>\n".data(using: .utf8)!)
    exit(64)
}

for path in files {
    print("===FILE\t\(path)")
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        FileHandle.standardError.write("无法读取: \(path)\n".data(using: .utf8)!)
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = false      // 关闭纠错，避免金额/时间被"修正"
    do {
        try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    } catch {
        FileHandle.standardError.write("识别失败: \(path)\n".data(using: .utf8)!)
        continue
    }
    for o in (req.results ?? []) {
        guard let c = o.topCandidates(1).first else { continue }
        let b = o.boundingBox                // Vision 原点在左下，转成左上
        let x = b.origin.x, y = 1.0 - b.origin.y - b.size.height
        print(String(format: "%@\t%.4f\t%.4f\t%.4f\t%.4f",
                     c.string, x, y, b.size.width, b.size.height))
    }
}
