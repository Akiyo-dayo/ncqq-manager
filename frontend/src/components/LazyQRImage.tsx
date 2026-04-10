/**
 * LazyQRImage — QR 图片懒加载组件（IntersectionObserver）
 *
 * Phase 5 优化：200+ 容器时，不在可视区域的 QR 图片不加载/不渲染 <img>。
 * 进入视口后才开始渲染真实图片，离开视口后可选保留（避免重新加载）。
 */
import { useRef, useState, useEffect, type CSSProperties } from 'react';

interface LazyQRImageProps {
    src: string;
    alt?: string;
    width?: number | string;
    height?: number | string;
    style?: CSSProperties;
    className?: string;
    /** 提前加载偏移量 (px)，默认 100 */
    rootMargin?: string;
    /** 占位符元素（默认灰色方块） */
    placeholder?: React.ReactNode;
    onClick?: () => void;
}

export default function LazyQRImage({
    src,
    alt = 'QR Code',
    width = 140,
    height = 140,
    style,
    className,
    rootMargin = '100px',
    placeholder,
    onClick,
}: LazyQRImageProps) {
    const containerRef = useRef<HTMLDivElement>(null);
    const [isVisible, setIsVisible] = useState(false);

    useEffect(() => {
        const el = containerRef.current;
        if (!el) return;

        const observer = new IntersectionObserver(
            ([entry]) => {
                if (entry.isIntersecting) {
                    setIsVisible(true);
                    observer.disconnect(); // 一旦可见就不再监听
                }
            },
            { rootMargin },
        );
        observer.observe(el);
        return () => observer.disconnect();
    }, [rootMargin]);

    return (
        <div
            ref={containerRef}
            style={{ width, height, display: 'inline-block', ...style }}
            className={className}
            onClick={onClick}
        >
            {isVisible ? (
                <img
                    src={src}
                    alt={alt}
                    width={width}
                    height={height}
                    loading="lazy"
                    style={{ display: 'block', objectFit: 'contain' }}
                />
            ) : (
                placeholder || (
                    <div
                        style={{
                            width: '100%',
                            height: '100%',
                            backgroundColor: 'rgba(128,128,128,0.1)',
                            borderRadius: 8,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                        }}
                    />
                )
            )}
        </div>
    );
}

